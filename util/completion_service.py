import os
import subprocess
import uuid
import logging
import subprocess

logging.basicConfig()

from studio import runner, model


class CompletionServiceManager:
    def __init__(self, config=None, resources_needed=None, cloud=None, verbose=10):
        self.config = config
        self.experimentId = experimentId
        self.project_name = "completion_service_" + experimentId
        self.queue_name = project_name
        self.resources_needed = resources_needed
        self.wm = runner.get_worker_manager(config, cloud)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(verbose)

        self.queue = runner.get_queue(queue_name, self.cloud, verbose)

        self.completion_services = {}

    def submitTask(self, experimentId, clientCodeFile, args):
        if experimentId not in self.completion_services.keys():
            self.completion_services[experimentId] = \
                CompletionService(
                    experimentId, 
                    self.config, 
                    self.resources_needed,
                    self.cloud,
                    self.verbose).__enter__()

        return self.completion_services[experimentId].submitTask(clientCodeFile, args)

    
    def __enter__(self):
        return self

    def __exit__(self, *args):
        for _, cs in self.completion_services.iter_items():
            cs.__exit__()


class CompletionService:

    def __init__(self, experimentId, config=None, resources_needed=None, cloud=None, verbose=10):
        self.config = model.get_config(config)    
        self.cloud = None
        self.experimentId = experimentId
        self.project_name = "completion_service_" + experimentId
        self.queue_name = 'local'
        self.resources_needed = resources_needed
        self.wm = runner.get_worker_manager(config, cloud)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(verbose)

        self.queue = runner.get_queue(self.queue_name, self.cloud, verbose)

        self.bid = '100%'
        self.cloud_timeout = 100


    def __enter__(self):
        if self.wm:
            self.wm.start_spot_workers(
                    self.queue_name,
                    self.bid,
                    self.resources_needed,
                    start_workers=1,
                    queue_upscaling=True,
                    ssh_keypair='peterz-k1',
                    timeout=self.cloud_timeout)
        else:
            self.p = subprocess.Popen([
                  'studio-local-worker',
                  '--verbose=debug',
                  '--timeout=' + str(self.cloud_timeout)],
                  close_fds=True)
 
        return self

    def __exit__(self, *args):
        if self.queue_name != 'local':
            self.queue.delete()

        if self.p:
            self.p.wait()

    def submitTask(self, clientCodeFile, args):
        cwd = os.path.dirname(os.path.realpath(__file__)),
          
        artifacts = {
            'retval': {
                'mutable':True,
                'local':'./retval.pkl'
            },
            'clientscript': {
                'mutable': False,
                'local': clientCodeFile
            }
        }

        experiment_name = self.project_name + "_" + str(uuid.uuid4())   
        experiment = model.create_experiment(
            'completion_service_client.py',
            args,
            experiment_name=experiment_name,
            project=self.project_name,
            artifacts=artifacts,
            resources_needed=self.resources_needed)

        with model.get_db_provider(self.config) as db:
            db.add_experiment(experiment)
        
        import pdb
        pdb.set_trace()
        runner.submit_experiments(
            [experiment],
            config=self.config,
            logger=self.logger,
            cloud=self.cloud,
            queue_name=self.queue_name)

        return experiment_name              
              
           
    def submitTaskWithFiles():
        raise NotImplementedError

    def getResults():
        with model.get_db_provider(self.config) as db:
            experiments = db.get_project_experiments(self.project_name)
            
        retval = {}
        
        for e in experiments:
            artifact_fileobj = db.stream_artifact(e.artifacts['retval'])
            data = pickle.loads(artifact_fileobj.read())
        
            retval[e.key] = data
           
        return retval

    def getResultsWithTimeout():
        raise NotImplementedError

