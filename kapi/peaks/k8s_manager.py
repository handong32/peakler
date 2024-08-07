import time
from asyncio import Queue
from typing import Dict, List, Tuple

from kubernetes import client, config, watch
import logging
import os
import asyncio

from kubernetes.client import ApiException, V1Deployment, V1ContainerImage

logger = logging.getLogger(__name__)
#logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)-6s | %(filename)-10s | %(funcName)-20s || %(message)s')

LABEL_IS_WORKLOAD = "is_workload"

# Monkey patch for https://github.com/kubernetes-client/python/issues/895
def names(self, names):
    self._names = names
V1ContainerImage.names = V1ContainerImage.names.setter(names)

def load_k8s_config():
    if os.getenv('KUBERNETES_SERVICE_HOST'):
        logger.debug('Detected running inside cluster. Using incluster auth.')
        config.load_incluster_config()
    else:
        logger.debug('Using kube auth.')
        config.load_kube_config()


class K8sManager():
    def __init__(self,
                 update_loop_sleep_time: float = 1,
                 dry_run: bool = False,
                 namespace: str = 'default'):
        '''

        :param update_loop_sleep_time: Sleep time between state updates
        :param dry_run: bool to set whether to actually execute resource allocations or not. See apply_allocations.
        :param namespace: Kubernetes namespace to operate in
        '''
        self.update_loop_sleep_time = update_loop_sleep_time
        self.dry_run = dry_run
        self.namespace = namespace

        # Load config and create API objects
        load_k8s_config()
        self.coreapi = client.CoreV1Api()
        self.appsapi = client.AppsV1Api()

        # Create state variables and update state
        self.deployments = {}
        self.current_allocation = {}

    def get_pods(self):
        return self.coreapi.list_pod_for_all_namespaces(watch=False)

    def get_pods_ns(self, ns: str = 'default'):
        return self.coreapi.list_namespaced_pod(namespace=ns, watch=False)

    # Get the cluster nodes
    def get_nodes(self, spec=""):
        ret = []
        node_list = self.coreapi.list_node()
        for node in node_list.items:
            if spec in node.metadata.name:
                ret.append(node.metadata.name)
        return ret

    def list_pods(self):
        pods = self.get_pods()
        for i in pods.items:
            logger.info("list_pods: %s\t%s\t%s" % (i.status.pod_ip, i.metadata.namespace, i.metadata.name))

    def get_cluster_resources(self,
                              resource_label: str = 'cpu'):
        res = 0
        nodes = self.coreapi.list_node()
        for node in nodes.items:
            logger.info(f"node.metadata.name {node.metadata.name}")
            if "jobmanager" not in node.metadata.name:
                res += float(node.status.capacity[resource_label])
        """
        print(nodes)        
        for n in nodes.items:
            if 'dedicated' in n.metadata.labels:
                if n.metadata.labels['dedicated'] == 'scheduler':
                    continue    # Do not add node to resources list if it's a dedicated scheduling node.
            res += float(n.status.capacity[resource_label])
        """
        return res

    def update_local_state(self) -> Tuple[List[V1Deployment], List[V1Deployment]]:
        deps = self.get_deployments()
        # Compute diffs
        added_deployments = [d for dep_name, d in deps.items() if dep_name not in self.deployments.keys()]
        removed_deployments = [d for dep_name, d in self.deployments.items() if dep_name not in deps.keys()]

        # Update state
        self.deployments = deps
        self.current_allocation = {d.metadata.name: d.status.replicas for dep_name, d in deps.items()}

        return added_deployments, removed_deployments

    def all_pods_running(self):
        pods = self.get_pods_ns(self.namespace)
        for pod in pods.items:
            if 'root--' in pod.metadata.name:
                if pod.status.container_statuses[0].state.running is None:
                    logger.info(f"{pod.metadata.name} broken.")
                    return False
        return True
                    
    def get_running_pods(self):
        pods = self.get_pods_ns(self.namespace)
        for pod in pods.items:
            if 'root--' in pod.metadata.name:
                if pod.status.container_statuses[0].state.running is None:
                    print(pod.metadata.name, len(pod.status.container_statuses))
        
    def get_deployments(self):
        deps = self.appsapi.list_namespaced_deployment(namespace=self.namespace)
        deps = {d.metadata.name: d for d in deps.items if
                d.metadata.labels.get(LABEL_IS_WORKLOAD,
                                      "false").lower() == "true"}  # Filter only deployments which are actual workloads. This is to exclude cilantro client and app client deployments.
        return deps

    def scale_deployment_node(self,
                              name: str,
                              replicas: int,
                              ns: str = None,
                              node: str = None):
        """
        Scale a deployment up or down. The `name` is the name of the deployment.
        """
        if ns is None:
            ns = self.namespace
        
        if node is None:            
            body = {"spec": {"replicas": replicas}}
        else:
            body = {"spec":{"replicas": replicas, "template":{"spec":{"nodeName": node}}}}
            
        try:
            self.appsapi.patch_namespaced_deployment(name, namespace=ns, body=body)
        except ApiException as e:
            logger.error(f"Failed to scale '{name}' to {replicas} replicas: {str(e)}, node: {node}")
            raise e

    def scale_deployment(self,
                         name: str,
                         replicas: int,
                         ns: str = None):
        """
        Scale a deployment up or down. The `name` is the name of the deployment.
        """
        if ns is None:
            ns = self.namespace
        
        body = {"spec": {"replicas": replicas}}
        
        try:
            self.appsapi.patch_namespaced_deployment_scale(name, namespace=ns, body=body)
        except ApiException as e:
            logger.error(f"Failed to scale '{name}' to {replicas} replicas: {str(e)}")
            raise e

    def apply_allocation(self,
                         resource_allocations: Dict[str, int]):
        '''
        Applies a resource allocation vector to
        :param resource_allocations: Dictionary mapping applications to number of resources.
        :return:
        '''
        if not self.dry_run:
            for dep_name, allocation in resource_allocations.items():
                assert isinstance(allocation, int), \
                    f"Got allocation {allocation} type {type(allocation)}, was expecting int"
                if allocation != self.current_allocation[dep_name]:
                    self.scale_deployment(dep_name, allocation)
        else:
            logger.warning("Running in dry run mode. Not actually setting resource allocations.")


    def get_alloc_granularity(self,
                              resource_label: str = 'cpu'):
        """ Return the granularity of allocation, which is the minimum quantum of resources
            we can allocate for a job.
            Since our scaling mechanism is scaling by controlling the number of replicas, the
            granularity is 1 (fractional replicas don't make sense).
        """
        return 1

    """
    @staticmethod
    def _create_add_event(added_deployment: V1Deployment) -> AppAddEvent:
        def _get_float_or_default(d, key, default):
            try:
                return float(d[key])
            except (TypeError, ValueError, KeyError) as e:
                logger.warning(f"Unable to convert {key} to float or value was not found while parsing k8s labels. Using default {default}. Labels were {d}")
                return default
        timestamp = time.time()
        app_threshold = _get_float_or_default(added_deployment.metadata.labels, "threshold", 1)
        app_weight = _get_float_or_default(added_deployment.metadata.labels, "app_weight", 1)
        app_unit_demand = _get_float_or_default(added_deployment.metadata.labels, "app_unit_demand", 1)
        return AppAddEvent(app_path=added_deployment.metadata.name,
                           app_threshold=app_threshold,
                           app_weight=app_weight,
                           app_unit_demand=app_unit_demand,
                           timestamp=timestamp,
                           event_type=EventTypes.APP_ADDED)

    def _create_events(self,
                      added_deployments: List[V1Deployment],
                      removed_deployments: List[V1Deployment]):
        add_events = []
        for a in added_deployments:
            try:
                e = self._create_add_event(a)
                add_events.append(e)
            except Exception as exp:
                logger.error(f"K8s deployment {a.metadata.name} raised exception when creating AppAddEvent for cilantro. Are all fields correctly added to the k8s labels for this deployment? Error: {exp}. Deployment labels: {a.metadata.labels}.")
                raise exp

        remove_events = []
        timestamp = time.time()
        for r in removed_deployments:
            try:
                e = AppUpdateEvent(r.metadata.name, timestamp, event_type=EventTypes.APP_REMOVED)
                remove_events.append(e)
            except Exception as exp:
                logger.error(
                    f"K8s deployment {a.metadata.name} raised exception when creating AppUpdateEvent(remove) for cilantro. Are all fields correctly added to the k8s labels for this deployment? Error: f{str(exp)}. Deployment labels: f{a.metadata.labels}.")
                raise exp
        app_events = add_events + remove_events
        return app_events


    async def local_state_update_loop(self):
        while True:
            added_deployments, removed_deployments = self.update_local_state()
            app_events = self._create_events(added_deployments, removed_deployments)
            for e in app_events:
                await self.event_queue.put(e)
            await asyncio.sleep(self.update_loop_sleep_time)
    """

