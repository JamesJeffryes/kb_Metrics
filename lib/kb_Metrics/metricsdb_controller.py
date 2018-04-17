import warnings
import time
import datetime
import copy
import re

from redis_cache import cache_it_json

from kb_Metrics.metrics_dbi import MongoMetricsDBI
from kb_Metrics.Util import (_unix_time_millis_from_datetime,
                             _convert_to_datetime)
from Catalog.CatalogClient import Catalog


def log(message, prefix_newline=False):
    """
    Logging function, provides a hook to suppress or redirect log messages.
    """
    print(('\n' if prefix_newline else '') +
          '{0:.2f}'.format(time.time()) + ': ' + str(message))


class MetricsMongoDBController:

    def _config_str_to_list(self, list_str):

        user_list = list()
        if list_str:
            user_list = [x.strip() for x in list_str.split(',') if x.strip()]
        else:
            warnings.warn('no {} are set in config of'
                          ' MetricsMongoDBController'.format(list_str))

        return user_list

    def _is_admin(self, username):
        return username in self.adminList

    def _is_metrics_admin(self, username):
        return username in self.metricsAdmins

    def _is_kbstaff(self, username):
        return username in self.kbstaff_list

    def _convert_isodate_to_milis(self, src_list, dt_list):
        for src in src_list:
            for ldt in dt_list:
                if ldt in src and isinstance(src[ldt], datetime.datetime):
                    src[ldt] = _unix_time_millis_from_datetime(src[ldt])
        return src_list

    def _parse_app_id(self, et_jobinput):
        app_id = ''
        if 'app_id' in et_jobinput:
            app_id = et_jobinput['app_id'].replace('.', '/')

        return app_id

    def _parse_method(self, et_jobinput):
        method_id = ''
        if 'method' in et_jobinput:
            method_id = et_jobinput['method'].replace('/', '.')

        return method_id

    def _update_user_info(self, params, token):
        """
        update user info
        If match not found, insert that record as new.
        """
        params = self._process_parameters(params)
        auth2_ret = self.metrics_dbi.aggr_user_details(
            params['user_ids'], params['minTime'], params['maxTime'])
        up_dated = 0
        up_serted = 0
        if not auth2_ret:
            print("No user records returned for update!")
            return 0

        print('Retrieved {} user record(s) for update!'.format(len(auth2_ret)))
        id_keys = ['username', 'email']
        data_keys = ['full_name', 'signup_at', 'last_signin_at', 'roles']
        for u_data in auth2_ret:
            id_data = {x: u_data[x] for x in id_keys}
            user_data = {x: u_data[x] for x in data_keys}
            is_kbstaff = 1 if self._is_kbstaff(id_data['username']) else 0
            update_ret = self.metrics_dbi.update_user_records(
                id_data, user_data, is_kbstaff)
            if update_ret.raw_result['updatedExisting']:
                up_dated += update_ret.raw_result['nModified']
            elif update_ret.raw_result.get('upserted'):
                up_serted += 1
        print('updated {} and upserted {} users.'.format(up_dated, up_serted))
        return up_dated + up_serted

    def _update_daily_activities(self, params, token):
        """
        update user activities reported from Workspace.
        If match not found, insert that record as new.
        """
        ws_ret = self._get_activities_from_wsobjs(params, token)
        act_list = ws_ret['metrics_result']
        up_dated = 0
        up_serted = 0
        if not act_list:
            print("No daily activity records returned for update!")
            return 0

        print('Retrieved {} activity record(s) for '
              'update!'.format(len(act_list)))
        id_keys = ['_id']
        count_keys = ['obj_numModified']
        for a_data in act_list:
            id_data = {x: a_data[x] for x in id_keys}
            count_data = {x: a_data[x] for x in count_keys}
            update_ret = self.metrics_dbi.update_activity_records(
                id_data, count_data)
            if update_ret.raw_result['updatedExisting']:
                up_dated += update_ret.raw_result['nModified']
            elif update_ret.raw_result.get('upserted'):
                up_serted += 1

        print('updated {} and upserted {} '
              'activities.'.format(up_dated, up_serted))
        return up_dated + up_serted

    def _update_narratives(self, params, token):
        """
        update user narratives reported from Workspace.
        If match not found, insert that record as new.
        """
        ws_ret = self._get_narratives_from_wsobjs(params, token)
        narr_list = ws_ret['metrics_result']
        up_dated = 0
        up_serted = 0
        if not narr_list:
            print("No narrative records returned for update!")
            return 0

        print('Retrieved {} narratives record(s) for '
              'update!'.format(len(narr_list)))
        id_keys = ['object_id', 'object_version', 'workspace_id']
        other_keys = ['name', 'last_saved_at', 'last_saved_by', 'numObj',
                      'deleted', 'nice_name', 'desc']

        for n_data in narr_list:
            id_data = {x: n_data[x] for x in id_keys}
            other_data = {x: n_data[x] for x in other_keys}
            update_ret = self.metrics_dbi.update_narrative_records(
                id_data, other_data)
            if update_ret.raw_result['updatedExisting']:
                up_dated += update_ret.raw_result['nModified']
            elif update_ret.raw_result['upserted']:
                up_serted += 1

        print('updated {} and upserted {} '
              'narratives.'.format(up_dated, up_serted))
        return up_dated + up_serted

    # End functions to write to the metrics database

    # functions to get the requested records from other dbs...
    def _get_narratives_from_wsobjs(self, params, token):
        """
        _get_narratives_from_wsobjs--Given a time period, fetch the narrative
        information from workspace.workspaces and workspace.workspaceObjects.
        Based on the narratives in workspace.workspaceObjects, if additional
        info available then add to existing data from workspace.workspaces.
        """
        # get the ws_narrative for lookups
        if self.ws_narratives is None:
            self.ws_narratives = self.metrics_dbi.list_ws_narratives()
        if self.narrative_name_map == {}:
            self.narrative_name_map = self._get_narrative_map()

        params = self._process_parameters(params)

        ws_narrs = copy.deepcopy(self.ws_narratives)
        ws_ids = [wnarr['workspace_id'] for wnarr in ws_narrs]
        wsobjs = self.metrics_dbi.list_user_objects_from_wsobjs(
            params['minTime'], params['maxTime'], ws_ids)
        ws_narrs1 = []
        for wsn in ws_narrs:
            for obj in wsobjs:
                if wsn['workspace_id'] == obj['workspace_id']:
                    if wsn['name'] == obj['object_name']:
                        wsn[u'object_id'] = obj['object_id']
                        wsn[u'object_version'] = obj['object_version']
                        break
                    elif ':' in wsn['name']:
                        wts = wsn['name'].split(':')[1]
                        if '_' in wts:
                            wts = wts.split('_')[1]
                        p = re.compile(wts, re.IGNORECASE)
                        if p.search(obj['object_name']):
                            wsn[u'object_id'] = obj['object_id']
                            wsn[u'object_version'] = obj['object_version']
                        break
        for wsn in ws_narrs:
            if wsn.get('object_id'):
                wsn[u'last_saved_by'] = wsn.pop('username')
                wsn[u'nice_name'] = self.narrative_name_map.get(wsn['workspace_id'])
                ws_narrs1.append(wsn)
        return {'metrics_result': ws_narrs1}

    def _get_activities_from_wsobjs(self, params, token):

        params = self._process_parameters(params)

        wsobjs_act = self.metrics_dbi.aggr_activities_from_wsobjs(
            params['minTime'], params['maxTime'])
        ws_owners = self.metrics_dbi.list_ws_owners()

        for wo in ws_owners:
            for obj in wsobjs_act:
                if wo['ws_id'] == obj['_id']['ws_id']:
                    obj['_id'][u'username'] = wo['username']
                    break
        return {'metrics_result': wsobjs_act}

    def _join_task_ujs(self, exec_tasks, ujs_jobs):
        """
        combine/join exec_tasks with ujs_jobs list to get the final return data
        """
        ujs_ret = []
        for j in ujs_jobs:
            u_j_s = self._assemble_ujs_state(j, exec_tasks)

            ujs_ret.append(u_j_s)
        return ujs_ret

    @cache_it_json(limit=1024, expire=60 * 60 / 2)
    def _assemble_ujs_state(self, ujs, exec_tasks):
        u_j_s = copy.deepcopy(ujs)
        u_j_s['job_id'] = str(u_j_s.pop('_id'))
        u_j_s['exec_start_time'] = u_j_s.pop('started', None)
        u_j_s['creation_time'] = u_j_s.pop('created')
        u_j_s['modification_time'] = u_j_s.pop('updated')

        authparam = u_j_s.pop('authparam')
        authstrat = u_j_s.pop('authstrat')
        if authstrat == 'kbaseworkspace':
            u_j_s['wsid'] = authparam

        if u_j_s.get('desc'):
            desc = u_j_s.pop('desc').split()[-1]
            if '.' in desc:
                u_j_s['method'] = desc

        for exec_task in exec_tasks:
            if str(exec_task['ujs_job_id']) == u_j_s['job_id']:
                if 'job_input' in exec_task:
                    et_job_in = exec_task['job_input']
                    u_j_s['app_id'] = self._parse_app_id(et_job_in)
                    if not u_j_s.get('method'):
                        u_j_s['method'] = self._parse_method(et_job_in)
                    if not u_j_s.get('wsid'):
                        if 'wsid' in et_job_in:
                            u_j_s['wsid'] = et_job_in['wsid']
                        elif 'params' in et_job_in and et_job_in['params'] != []:
                            p_ws = et_job_in['params'][0]
                            if isinstance(p_ws, dict) and 'ws_id' in p_ws:
                                u_j_s['wsid'] = p_ws['ws_id']
                    # try to get the workspace_name--first nice_name,
                    # if na then job_task--because sometimes both nice_name and
                    # workspace (or workspace_name) exist, pick nice_name
                    if u_j_s.get('wsid'):
                        u_j_s['workspace_name'] = self.narrative_name_map.get(
                                                        int(u_j_s['wsid']))
                    if u_j_s.get('workspace_name', None) is None:
                        if 'params' in et_job_in and et_job_in['params'] != []:
                            p_ws = et_job_in['params'][0]
                            if isinstance(p_ws, dict):
                                if 'workspace' in p_ws:
                                    u_j_s['workspace_name'] = p_ws['workspace']
                                elif 'workspace_name' in p_ws:
                                    u_j_s['workspace_name'] = p_ws['workspace_name']
                    if ('workspace_name' in u_j_s and
                            u_j_s['workspace_name'] is None):
                        u_j_s.pop('workspace_name')
                break

        if not u_j_s.get('app_id') and u_j_s.get('method'):
            u_j_s['app_id'] = u_j_s['method'].replace('.', '/')

        if (not u_j_s.get('finish_time') and
                not u_j_s.get('error') and
                u_j_s.get('complete')):
            u_j_s['finish_time'] = u_j_s.pop('modification_time')

        # get the narrative name if any
        if u_j_s.get('wsid'):
            n_nm = self.narrative_name_map.get(int(u_j_s['wsid']))
            if n_nm != "":
                u_j_s['narrative_name'] = n_nm

        # get the client groups
        u_j_s['client_groups'] = ['njs']  # default client groups to 'njs'
        if self.client_groups:
            for clnt in self.client_groups:
                clnt_id = clnt['app_id']
                ujs_a_id = str(u_j_s.get('app_id'))
                if str(clnt_id).lower() == ujs_a_id.lower():
                    u_j_s['client_groups'] = clnt['client_groups']
                    break

        return u_j_s

    def _process_parameters(self, params):
        params['user_ids'] = params.get('user_ids', [])

        if not isinstance(params['user_ids'], list):
            raise ValueError('Variable user_ids must be a list.')

        if 'kbasetest' in params['user_ids']:
            params['user_ids'].remove('kbasetest')

        epoch_range = params.get('epoch_range')
        if epoch_range:
            if len(epoch_range) != 2:
                raise ValueError('Invalide epoch_range. Size must be 2.')
            start_time, end_time = epoch_range
            if start_time and end_time:
                start_time = _convert_to_datetime(start_time)
                end_time = _convert_to_datetime(end_time)
            elif start_time and not end_time:
                start_time = _convert_to_datetime(start_time)
                end_time = start_time + datetime.timedelta(hours=48)
            elif not start_time and end_time:
                end_time = _convert_to_datetime(end_time)
                start_time = end_time - datetime.timedelta(hours=48)
            else:
                end_time = datetime.datetime.utcnow()
                start_time = end_time - datetime.timedelta(hours=48)
        else:  # set the most recent 48 hours range
            end_time = datetime.datetime.utcnow()
            start_time = end_time - datetime.timedelta(hours=48)

        params['minTime'] = _unix_time_millis_from_datetime(start_time)
        params['maxTime'] = _unix_time_millis_from_datetime(end_time)

        return params

    @cache_it_json(limit=1024, expire=60 * 60 / 2)
    def _get_narrative_map(self):
        """
        _get_narrative_map: Fetch the narrative id and name
        (or narrative_nice_name if it exists) into a dictionary
        of {key=ws_id, value=ws_name}
        """
        # 1. get the narr_owners data to start
        if self.ws_narratives is None:
            self.ws_narratives = self.metrics_dbi.list_ws_narratives()

        # 2. loop through all self.ws_narratives
        narrative_name_map = {}
        for narr in self.ws_narratives:
            narrative_name_map[narr['workspace_id']] = narr['name']  # default
            n_keys = narr['narr_keys']
            n_vals = narr['narr_values']
            for i in range(0, len(n_keys)):
                if n_keys[i] == 'narrative_nice_name':
                    narrative_name_map[narr['workspace_id']] = n_vals[i]
                    break
        return narrative_name_map

    @cache_it_json(limit=1024, expire=60 * 60 * 1)
    def _get_client_groups_from_cat(self, token):
        """
        _get_client_groups_from_cat: Get the client_groups data from Catalog API
        return an array of the following structure (example with data):
        {
            u'app_id': u'assemblyrast/run_arast',
            u'client_groups': [u'bigmemlong'],
            u'function_name': u'run_arast',
            u'module_name': u'AssemblyRAST'},
        }
        """
        # initialize client(s) for accessing other services
        self.cat_client = Catalog(self.catalog_url,
                                  auth_svc=self.auth_service_url, token=token)
        # Pull the data
        client_groups = self.cat_client.get_client_groups({})

        return [{'app_id': client_group.get('app_id'),
                 'client_groups': client_group.get('client_groups')}
                for client_group in client_groups]

    @cache_it_json(limit=1024, expire=60 * 60 * 1)
    def _get_narrative_info(self, params):
        # 1. get the narr_owners data for lookups
        if self.narr_data is None:
            self.narr_data = self.metrics_dbi.list_narrative_info()

        # 2. query db to get lists of narratives with ws_ids and first_access_date
        params = self._process_parameters(params)
        ws_firstAccs = self.metrics_dbi.list_ws_firstAccess(
                            params['minTime'],
                            params['maxTime'])

        # 3. match the narrative owners and assemble the info
        narr_info_list = []
        for narr_info in self.narr_data:
            narr = {}
            for wsobj in ws_firstAccs:
                if narr_info['ws'] == wsobj['ws']:
                    narr['ws'] = narr_info['ws']
                    narr['name'] = narr_info['name']
                    narr['owner'] = narr_info['owner']
                    narr['first_access'] = wsobj['yyyy-mm-dd']
                    narr_info_list.append(narr)
                    break

        return narr_info_list

    def __init__(self, config):
        # grab config lists
        self.adminList = self._config_str_to_list(
            config.get('admin-users'))
        self.metricsAdmins = self._config_str_to_list(
            config.get('metrics-admins'))
        self.mongodb_dbList = self._config_str_to_list(
            config.get('mongodb-databases'))

        # check for required parameters
        for p in ['mongodb-host', 'mongodb-databases',
                  'mongodb-user', 'mongodb-pwd']:
            if p not in config:
                error_msg = '"{}" config variable must be defined '.format(p)
                error_msg += 'to start a MetricsMongoDBController!'
                raise ValueError(error_msg)

        # instantiate the mongo client
        self.metrics_dbi = MongoMetricsDBI(config.get('mongodb-host'),
                                           self.mongodb_dbList,
                                           config.get('mongodb-user', ''),
                                           config.get('mongodb-pwd', ''))

        # for access to the Catalog API
        self.auth_service_url = config['auth-service-url']
        self.catalog_url = config['kbase-endpoint'] + '/catalog'

        # commonly used data
        self.kbstaff_list = None
        self.ws_narratives = None
        self.client_groups = None
        self.cat_client = None
        self.narr_data = None
        self.narrative_name_map = {}

    def get_user_job_states(self, requesting_user, params, token):
        """
        get_user_job_states--generate data for appcatalog/stats from querying
        execution_engine, userjobstates, catalog and workspace
        ----------------------
        To get the job's 'status', 'complete'=true/false, etc.,
        we can do joining as follows
        --userjobstate.jobstate['_id']==exec_engine.exec_tasks['ujs_job_id']
        """
        if not self._is_admin(requesting_user):
            params['user_ids'] = [requesting_user]

        # 1. get the ws_narrative and client_groups data for lookups
        if self.ws_narratives is None:
            self.ws_narratives = self.metrics_dbi.list_ws_narratives()
        if self.client_groups is None:
            self.client_groups = self._get_client_groups_from_cat(token)
        if self.narrative_name_map == {}:
            self.narrative_name_map = self._get_narrative_map()

        # 2. query dbs to get lists of tasks and jobs
        params = self._process_parameters(params)
        exec_tasks = self.metrics_dbi.list_exec_tasks(params['minTime'],
                                                      params['maxTime'])
        ujs_jobs = self.metrics_dbi.list_ujs_results(params['user_ids'],
                                                     params['minTime'],
                                                     params['maxTime'])
        ujs_jobs = self._convert_isodate_to_milis(
            ujs_jobs, ['created', 'started', 'updated'])

        return {'job_states': self._join_task_ujs(exec_tasks, ujs_jobs)}

    def get_narrative_stats(self, requesting_user, params, token):
        """
        get_narrative_stats--generate narrative stats data for reporting purposes
        [{'owner': u'vkumar', 'ws': 8768, 'name': u'vkumar:1468592344827',
        'first_access': u'2016-7-15'},
        {'owner': u'psdehal', 'ws': 27834, 'name': u'psdehal:narrative_1513709108341',
        'first_access': u'2017-12-21'}]
        """
        if not self._is_admin(requesting_user):
                raise ValueError('You do not have permisson to '
                                 'invoke this action.')
        narr_info = self._get_narrative_info(params)

        return narr_info

    # function(s) to update the metrics db
    def update_metrics(self, requesting_user, params, token):
        """
        update_metrics--updates the metrics db collections
        """
        if not self._is_metrics_admin(requesting_user):
            raise ValueError('You do not have permission to '
                             'invoke this action.')

        if self.kbstaff_list is None:
            self.kbstaff_list = self.metrics_dbi.list_kbstaff_usernames()

        # 1. update users
        action_result1 = self._update_user_info(params, token)

        # 2. update activities
        action_result2 = self._update_daily_activities(params, token)

        # 3. update narratives
        action_result3 = self._update_narratives(params, token)

        return {'metrics_result': {'user_updates': action_result1,
                                   'activity_updates': action_result2,
                                   'narrative_updates': action_result3}}

    # functions to get the requested records from metrics db...
    def get_active_users_counts(self, requesting_user,
                                params, token, exclude_kbstaff=True):
        """
        get_active_users_counts--query (and aggregate) the metrics mongodb
        to get active user count per day.
        """
        if not self._is_metrics_admin(requesting_user):
            raise ValueError('You do not have permission to view this data.')

        if self.kbstaff_list is None:
            self.kbstaff_list = self.metrics_dbi.list_kbstaff_usernames()

        params = self._process_parameters(params)

        if exclude_kbstaff:
            mt_ret = self.metrics_dbi.aggr_unique_users_per_day(
                params['minTime'], params['maxTime'], self.kbstaff_list)
        else:
            mt_ret = self.metrics_dbi.aggr_unique_users_per_day(
                params['minTime'], params['maxTime'], [])

        if not mt_ret:
            print("No records returned!")

        return {'metrics_result': mt_ret}

    def get_user_details(self, requesting_user, params, token,
                         exclude_kbstaff=False):
        """
        get_user_details--query the metrics/users db to retrieve user info.
        """
        if not self._is_metrics_admin(requesting_user):
            raise ValueError('You do not have permission to view this data.')

        params = self._process_parameters(params)
        mt_ret = self.metrics_dbi.get_user_info(
            params['user_ids'], params['minTime'],
            params['maxTime'], exclude_kbstaff)

        if not mt_ret:
            print("No records returned!")
        else:
            mt_ret = self._convert_isodate_to_milis(
                mt_ret, ['signup_at', 'last_signin_at'])
        return {'metrics_result': mt_ret}

    # End functions to get the requested records from metrics db
