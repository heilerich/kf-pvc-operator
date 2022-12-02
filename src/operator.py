import os
import re
import functools
import textwrap
from pathlib import Path

from collections import OrderedDict
from copy import deepcopy
from uuid import uuid4

import kopf
import kubernetes
import yaml

def namespace_filter(labels, memo, **_):
    ns_filter = memo.ns_filter
    for key, value in ns_filter.items():
        if key not in labels:
            return False
        if value != labels[key]:
            return False
    return True

@kopf.on.login()
def login_fn(**kwargs):
    return kopf.login_via_client(**kwargs)

@kopf.on.startup()
def startup_fn(memo, logger, **_):
    kubeconfig_path = os.getenv('KUBECONFIG', default=None)
    if kubeconfig_path is not None:
        logger.info(f"Auth: Using kube config {kubeconfig_path}")
        kubernetes.config.load_kube_config()
    else:
        logger.info(f"Auth: Using incluster service account")
        kubernetes.config.load_incluster_config()

    ns_filter = dict()
    selector_str = os.getenv('NAMESPACE_SELECTOR', default=None)
    if selector_str is None:
        logger.warning('No namespace selector found. Will create objects in all namespaces.')
        return
    selectors = selector_str.split(',')
    for selector in selectors:
        selector = selector_str.split('=')
        if len(selector) != 2:
            raise kopf.PermanentError(f"Namespace selector '{selector_str}' is invalid. The selector "
                                       "should be formatted label=value")
        label, value = selector
        ns_filter[label] = value

    selector_str = ','.join([f"{label}={value}" for label, value in ns_filter.items()])
    logger.info(f"Namespace selector: {selector_str}")
    memo.ns_filter = ns_filter

    files = list(sorted([entry.path for entry in os.scandir('/templates') 
        if entry.is_file() and (entry.name.endswith('.yaml') or entry.name.endswith('.yml')) ]))
    texts = [Path(file).read_text() for file in files]
    templates = [yaml.safe_load(text) for text in texts]
    logger.info(f"Found {len(templates)} template(s)")
    
    if len(templates) <= 0:
        raise kopf.PermanentError("No templates found.")

    memo.templates = templates

    memo.resources = get_resource_types()

@kopf.timer('namespaces', when=namespace_filter, interval=10)
def ensure_objects(name, memo, logger, **_):
    logger.info(f"Reconciling {name}")
    handlers = {}
    for template in memo.templates:
        body = deepcopy(template)
        patch_or_create(target_ns=name, desired_body=body, logger=logger, memo=memo)

@kopf.on.create('v1', 'persistentvolumeclaims', annotations={'nail.science/nfs-pv': kopf.PRESENT}) 
def handle_pvc_creation(namespace, name, annotations, body, logger, **_):
    nfs_url = annotations['nail.science/nfs-pv']
    host = nfs_url.split('/')[0]
    path = '/' + '/'.join(nfs_url.split('/')[1:])

    pv_name = str(uuid4())

    api = kubernetes.client.CoreV1Api()
    text = textwrap.dedent(f"""\
    apiVersion: v1
    kind: PersistentVolume
    metadata:
      name: pvc-{pv_name}
      labels:
        manual-pv: nfs
    spec:
      capacity:
        storage: 1Gi
      volumeMode: Filesystem
      accessModes:
        - ReadWriteMany
      mountOptions:
        - noatime
        - nfsvers=3
        - fsc
      claimRef:
        namespace: {namespace}
        name: {name}
      nfs:
        path: {path}
        server: {host}
    """)
    obj = yaml.safe_load(text)
    api_obj = api.create_persistent_volume(body=obj)
    logger.info(f"Created PV {pv_name}")

@kopf.on.field('v1', 'persistentvolumes', field='status.phase', labels={'manual-pv': kopf.PRESENT}) 
def handle_pv_change(name, status, logger, **_):
    if 'phase' not in status or status['phase'] != 'Released':
        return
    api = kubernetes.client.CoreV1Api()
    api.delete_persistent_volume(name)
    logger.info(f"Deleted released PV {name}")

def get_resource_types():
    api_client = kubernetes.client.ApiClient()
    auth = kubernetes.client.Configuration().get_default_copy().auth_settings()
    apis_api = kubernetes.client.ApisApi()
    group_list = apis_api.get_api_versions()
    apis = [(g.name, gv.version) for g in group_list.groups for gv in g.versions]
    resources = {(name, version, r.kind): r.name 
                 for name, version in apis
                 for r in api_client.call_api(f"/apis/{name}/{version}/", 'GET', 
                                              auth_settings=auth,
                                              response_type=kubernetes.client.V1APIResourceList)[0]\
                                                      .resources if '/' not in r.name}
    resources.update({
        ('', 'v1', r.kind): r.name
        for r in api_client.call_api(f"/api/v1", 'GET', 
            auth_settings=auth,
            response_type=kubernetes.client.V1APIResourceList)[0].resources if '/' not in r.name
    })
    return resources

def extract_endpoint(body, memo, logger):
    api = body['apiVersion'].split('/')
    group = api[0] if len(api) == 2 else ''
    version = api[0] if len(api) == 1 else api[1]
    kind = body['kind']
    namespace = body['metadata']['namespace']

    if (group, version, kind) not in memo.resources:
        logger.info(f"Could not map kind '{kind}' ({group}/{version}) to resource endpoint."
                     "Refreshing endpoint list")
        memo.resources = get_resource_types(memo.api_client)
        if (group, version, kind) not in memo.resources:
            logger.debug(f"Known resources {memo.resources}")
            raise Exception(f"Could not map kind '{kind}' ({group}/{version}) to resource endpoint.")

    plural = memo.resources[(group, version, kind)]

    return dict(namespace=namespace, group=group, version=version, plural=plural)

def api_function(method, body, endpoint):
    auth = kubernetes.client.Configuration().get_default_copy().auth_settings()
    if endpoint['group'] == "":
        if method == 'get':
            api_client = kubernetes.client.ApiClient()
            def getter(name, **_):
                return api_client.call_api(
                    "/api/v1/namespaces/{namespace}/{plural}/{name}".format(name=name, **endpoint),
                    'GET',
                    auth_settings=auth,
                    response_type='object')[0]
            return getter
        else:
            api = kubernetes.client.CoreV1Api()
            kind = body['kind']
            kind = re.compile('(.)([A-Z][a-z]+)').sub(r'\1_\2', kind)
            kind = re.compile('([a-z0-9])([A-Z])').sub(r'\1_\2', kind).lower()
            fn = getattr(api, f"{method}_namespaced_{kind}")
            return functools.partial(fn, namespace=endpoint['namespace'])
    else:
        api = kubernetes.client.CustomObjectsApi()
        fn = getattr(api, f"{method}_namespaced_custom_object")
        return functools.partial(fn, **endpoint)

def patch_or_create(target_ns, desired_body, memo, logger):
    name = desired_body['metadata']['name']
    desired_body['metadata']['namespace'] = target_ns 
    endpoint = extract_endpoint(desired_body, memo, logger)

    try:
        existing_object = api_function('get', desired_body, endpoint)(name=name)
    except kubernetes.client.exceptions.ApiException as ex:
        if ex.status != 404:
            raise ex
        existing_object = None

    logger.debug(f"Existing object {existing_object}")

    if existing_object is None:
        create_object(endpoint, desired_body, logger)
    else:
        patch_object(endpoint, existing_object, desired_body, logger)

def create_object(endpoint, desired_body, logger):
    namespace = endpoint['namespace']
    name = desired_body['metadata']['name']
    logger.info(f"Creating {namespace}/{name}")

    api_function('create', desired_body, endpoint)(body=desired_body)

def patch_object(endpoint, existing_object, desired_body, logger):
    namespace = endpoint['namespace']
    name = desired_body['metadata']['name']

    def clean_object(obj):
        new = deepcopy(obj)
        if 'status' in new:
            del new['status']
        if 'spec' in new and 'volumeName' in new['spec']:
            del new['spec']['volumeName']
        new_meta = dict( 
            name=new['metadata']['name'],
            namespace=new['metadata']['namespace']
        )
        if 'labels' in new['metadata']:
            new_meta['labels'] = new['metadata']['labels']
        new['metadata'] = new_meta
        return new

    if clean_object(existing_object) == clean_object(desired_body):
        logger.info(f"Object {namespace}/{name} did not change. Nothing to do.")
        return
    logger.info(f"State {clean_object(existing_object)}")
    logger.info(f"Target {clean_object(desired_body)}")

    logger.info(f"Replace {namespace}/{name}")
    api_function('replace', desired_body, endpoint)(body=desired_body, name=name)
