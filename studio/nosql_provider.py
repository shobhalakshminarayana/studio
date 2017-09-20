import pyrebase
import logging

from firebase_artifact_store import FirebaseArtifactStore
from auth import FirebaseAuth

logging.basicConfig()

class NoSQLProvider(object):
    """Data provider for Firebase."""

    def __init__(self, db_config, blocking_auth=True, verbose=10, store=None):
        guest = db_config.get('guest')

        self.app = pyrebase.initialize_app(db_config)
        self.logger = logging.getLogger('FirebaseProvider')
        self.logger.setLevel(verbose)

        self.auth = None
        if not guest and 'serviceAccount' not in db_config.keys():
            self.auth = FirebaseAuth(self.app,
                                     db_config.get("use_email_auth"),
                                     db_config.get("email"),
                                     db_config.get("password"),
                                     blocking_auth)

        self.store = store if store else FirebaseArtifactStore(
            db_config, verbose=verbose, blocking_auth=blocking_auth)

        if self.auth and not self.auth.expired:
            self.__setitem__(self._get_user_keybase() + "email",
                             self.auth.get_user_email())

        self.max_keys = db_config.get('max_keys', 100)

   
    def _get_userid(self):
        userid = None
        if self.auth:
            userid = self.auth.get_user_id()
        userid = userid if userid else 'guest'
        return userid

    def _get_user_keybase(self, userid=None):
        if userid is None:
            userid = self._get_userid()

        return "users/" + userid + "/"

    def _get_experiments_keybase(self, userid=None):
        return "experiments/"

    def _get_projects_keybase(self):
        return "projects/"

    def add_experiment(self, experiment, userid=None):
        self._delete(self._get_experiments_keybase() + experiment.key)
        experiment.time_added = time.time()
        experiment.status = 'waiting'

        if 'local' in experiment.artifacts['workspace'].keys() and \
                os.path.exists(experiment.artifacts['workspace']['local']):
            experiment.git = git_util.get_git_info(
                experiment.artifacts['workspace']['local'])

        for tag, art in experiment.artifacts.iteritems():
            if art['mutable']:
                art['key'] = self._get_experiments_keybase() + \
                    experiment.key + '/' + tag + '.tgz'
            else:
                if 'local' in art.keys():
                    # upload immutable artifacts
                    art['key'] = self.store.put_artifact(art)

            if 'key' in art.keys():
                art['qualified'] = self.store.get_qualified_location(
                    art['key'])

            art['bucket'] = self.store.get_bucket()

        userid = userid if userid else self._get_userid()

        experiment_dict = experiment.__dict__.copy()
        experiment_dict['owner'] = userid

        self.__setitem__(self._get_experiments_keybase() + experiment.key,
                         experiment_dict)

        self.__setitem__(self._get_user_keybase(userid) + "experiments/" +
                         experiment.key,
                         experiment.time_added)

        if experiment.project and self.auth:
            self.__setitem__(self._get_projects_keybase() +
                             experiment.project + "/" +
                             experiment.key + "/owner",
                             userid)

        self.checkpoint_experiment(experiment, blocking=True)
        self.logger.info("Added experiment " + experiment.key)

    def start_experiment(self, experiment):
        experiment.time_started = time.time()
        experiment.status = 'running'
        self.__setitem__(self._get_experiments_keybase() +
                         experiment.key + "/status",
                         "running")

        self.__setitem__(self._get_experiments_keybase() +
                         experiment.key + "/time_started",
                         experiment.time_started)

        self.checkpoint_experiment(experiment)

    def stop_experiment(self, key):
        # can be called remotely (the assumption is
        # that remote worker checks experiments status periodically,
        # and if it is 'stopped', kills the experiment.
        if isinstance(key, Experiment):
            key = key.key

        self.__setitem__(self._get_experiments_keybase() +
                         key + "/status",
                         "stopped")

    def finish_experiment(self, experiment):
        time_finished = time.time()
        if isinstance(experiment, basestring):
            key = experiment
        else:
            key = experiment.key
            self.checkpoint_experiment(experiment, blocking=True)
            experiment.status = 'finished'
            experiment.time_finished = time_finished

        self.__setitem__(self._get_experiments_keybase() +
                         key + "/status",
                         "finished")

        self.__setitem__(self._get_experiments_keybase() +
                         key + "/time_finished",
                         time_finished)

    def delete_experiment(self, experiment):
        if isinstance(experiment, basestring):
            experiment_key = experiment
            try:
                experiment = self.get_experiment(experiment)
                experiment_key = experiment.key
            except BaseException:
                experiment = None
        else:
            experiment_key = experiment.key

        self._delete(self._get_user_keybase() + 'experiments/' +
                     experiment_key)
        if experiment is not None:
            for tag, art in experiment.artifacts.iteritems():
                if art.get('key') is not None:
                    self.logger.debug(
                        ('Deleting artifact {} from the store, ' +
                         'artifact key {}').format(tag, art['key']))
                    self.store.delete_artifact(art)

            if experiment.project is not None:
                self._delete(
                    self._get_projects_keybase() +
                    experiment.project +
                    "/" +
                    experiment_key)

        self._delete(self._get_experiments_keybase() + experiment_key)

    def checkpoint_experiment(self, experiment, blocking=False):
        if isinstance(experiment, basestring):
            key = experiment
            experiment = self.get_experiment(key, getinfo=False)
        else:
            key = experiment.key

        checkpoint_threads = [
            Thread(
                target=self.store.put_artifact,
                args=(art,))
            for _, art in experiment.artifacts.iteritems()
            if art['mutable'] and art.get('local')]

        for t in checkpoint_threads:
            t.start()

        self.__setitem__(self._get_experiments_keybase() +
                         key + "/time_last_checkpoint",
                         time.time())
        if blocking:
            for t in checkpoint_threads:
                t.join()
        else:
            return checkpoint_threads

    def _get_experiment_info(self, experiment):
        info = {}
        type_found = False

        if not type_found:
            info['type'] = 'unknown'

        info['logtail'] = self._get_experiment_logtail(experiment)

        if experiment.metric is not None:
            metric_str = experiment.metric.split(':')
            metric_name = metric_str[0]
            metric_type = metric_str[1] if len(metric_str) > 1 else None

            tbtar = self.store.stream_artifact(experiment.artifacts['tb'])

            if metric_type == 'min':
                def metric_accum(x, y): return min(x, y) if x else y
            elif metric_type == 'max':
                def metric_accum(x, y): return max(x, y) if x else y
            else:
                def metric_accum(x, y): return y

            metric_value = None
            for f in tbtar:
                if f.isreg():
                    for e in util.event_reader(tbtar.extractfile(f)):
                        for v in e.summary.value:
                            if v.tag == metric_name:
                                metric_value = metric_accum(
                                    metric_value, v.simple_value)

            info['metric_value'] = metric_value

        return info

    def _get_experiment_logtail(self, experiment):
        try:
            tarf = self.store.stream_artifact(experiment.artifacts['output'])
            if not tarf:
                return None

            logdata = tarf.extractfile(tarf.members[0]).read()
            logdata = util.remove_backspaces(logdata).split('\n')
            return logdata
        except BaseException as e:
            self.logger.info('Getting experiment logtail raised an exception:')
            self.logger.info(e)
            return None

    def get_experiment(self, key, getinfo=True):
        data = self._get(self._get_experiments_keybase() + key)
        assert data, "data at path %s not found! " % (
            self._get_experiments_keybase() + key)
        data['key'] = key

        experiment_stub = experiment_from_dict(data)

        expinfo = {}
        if getinfo:
            try:
                expinfo = self._get_experiment_info(experiment_stub)

            except Exception as e:
                self.logger.info(
                    "Exception {} while info download for {}".format(
                        e, key))

        return experiment_from_dict(data, expinfo)

    def get_user_experiments(self, userid=None, blocking=True):
        if userid and '@' in userid:
            users = self.get_users()
            user_ids = [u for u in users if users[u].get('email') == userid]
            if len(user_ids) < 1:
                return None
            else:
                userid = user_ids[0]

        experiment_keys = self._get(
            self._get_user_keybase(userid) + "/experiments")
        if not experiment_keys:
            experiment_keys = {}

        keys = sorted(experiment_keys.keys(),
                      key=lambda k: experiment_keys[k],
                      reverse=True)

        return keys

    def get_project_experiments(self, project):
        experiment_keys = self._get(self._get_projects_keybase() +
                                    project)
        if not experiment_keys:
            experiment_keys = {}

        return experiment_keys

    def get_artifacts(self, key):
        experiment = self.get_experiment(key, getinfo=False)
        retval = {}
        if experiment.artifacts is not None:
            for tag, art in experiment.artifacts.iteritems():
                url = self.store.get_artifact_url(art)
                if url is not None:
                    retval[tag] = url

        return retval

    def get_artifact(self, artifact, only_newer=True):
        return self.store.get_artifact(artifact, only_newer=only_newer)

    def _get_valid_experiments(self, experiment_keys,
                               getinfo=False, blocking=True):

        if self.max_keys > 0:
            experiment_keys = experiment_keys[:self.max_keys]

        def cache_valid_experiment(key):
            try:
                self._experiment_cache[key] = self.get_experiment(
                    key, getinfo=getinfo)
            except BaseException:
                self.logger.warn(
                    ("Experiment {} does not exist " +
                     "or is corrupted, try to delete record").format(key))
                try:
                    self.delete_experiment(key)
                except BaseException:
                    pass

        if self.pool:
            if blocking:
                self.pool.map(cache_valid_experiment, experiment_keys)
            else:
                self.pool.map_async(cache_valid_experiment, experiment_keys)
        else:
            for e in experiment_keys:
                cache_valid_experiment(e)

        return [self._experiment_cache[key] for key in experiment_keys
                if key in self._experiment_cache.keys()]

    def get_projects(self):
        return self._get(self._get_projects_keybase(), shallow=True)

    def get_users(self):
        user_ids = self._get('users/', shallow=True)
        retval = {}
        for user_id in user_ids.keys():
            retval[user_id] = {
                'email': self._get('users/' + user_id + '/email')
            }
        return retval

    def refresh_auth_token(self, email, refresh_token):
        if self.auth:
            self.auth.refresh_token(email, refresh_token)

    def is_auth_expired(self):
        if self.auth:
            return self.auth.expired
        else:
            return False

    def can_write_experiment(self, key=None, user=None):
        assert key is not None
        user = user if user else self._get_userid()

        owner = self._get(
            self._get_experiments_keybase() + key + "/owner")
        if owner is None or owner == 'guest':
            return True
        else:
            return (owner == user)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.app:
            self.app.requests.close()


