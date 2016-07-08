#!/usr/bin/env python
"""Signing script
"""
import aiohttp
import asyncio
import logging
import os
import ssl
import sys
import traceback
from urllib.parse import urlparse

import scriptworker.client
from scriptworker.context import Context
from scriptworker.exceptions import ScriptWorkerTaskException
from signingscript.task import task_cert_type, task_signing_formats, validate_task_schema
from signingscript.utils import load_json, load_signing_server_config
from signingscript.worker import copy_to_artifact_dir, download_files, get_token, read_temp_creds, sign_file

log = logging.getLogger(__name__)


# SigningContext {{{1
class SigningContext(Context):
    signing_servers = None

    def __init__(self):
        super(SigningContext, self).__init__()

    def write_json(self, *args):
        pass


# async_main {{{1
async def async_main(context):
    loop = asyncio.get_event_loop()
    work_dir = context.config['work_dir']
    context.task = scriptworker.client.get_task(context.config)
    log.debug(context.task)
    temp_creds_future = loop.create_task(read_temp_creds(context))
    log.debug("validating task")
    validate_task_schema(context)
    context.signing_servers = load_signing_server_config(context)
    log.debug(context.signing_servers)
    cert_type = task_cert_type(context.task)
    log.debug(cert_type)
    signing_formats = task_signing_formats(context.task)
    log.debug(signing_formats)
    # _ scriptworker needs to validate CoT artifact
    filelist = await download_files(context)
    log.debug("getting token")
    await get_token(context, os.path.join(work_dir, 'token'), cert_type, signing_formats)
    # TODO async?
    for filename in filelist:
        log.debug("signing %s", filename)
        # TODO .asc only if we're gpg
        artifacts = [filename, "{}.asc".format(filename)]
        await sign_file(context, os.path.join(work_dir, filename),
                        cert_type, signing_formats, context.config["ssl_cert"],
                        to=os.path.join(work_dir, "{}.asc".format(filename)))
        for source in artifacts:
            copy_to_artifact_dir(context, os.path.join(work_dir, source))
    # TODO manifest
    temp_creds_future.cancel()
    log.info("Done!")


# config {{{1
def get_default_config():
    """ Create the default config to work from.

    `my_ip` is special: when working with a docker signing server, the default
    ip becomes the .1 of the docker subnet.
    """
    if os.environ.get('DOCKER_HOST'):
        # The .1 on the same subnet as the DOCKER_HOST ip
        parsed = urlparse(os.environ['DOCKER_HOST'])
        parts = parsed.hostname.split('.')
        parts[3] = "1"
        my_ip = '.'.join(parts)
    else:
        my_ip = "127.0.0.1"
    cwd = os.getcwd()
    parent_dir = os.path.dirname(cwd)
    default_config = {
        'signing_server_config': 'server_config.json',
        'tools_dir': os.path.join(parent_dir, 'build-tools'),
        'work_dir': os.path.join(parent_dir, 'work_dir'),
        'artifact_dir': os.path.join(parent_dir, '/src/signing/artifact_dir'),
        'temp_creds_refresh_seconds': 330,
        'my_ip': my_ip,
        'ssl_cert': None,
        'signtool': "signtool",
        'schema_file': os.path.join(cwd, 'signingscript', 'data', 'signing_task_schema.json'),
        'verbose': True,
    }
    return default_config


# main {{{1
def usage():
    print("Usage: {} CONFIG_FILE".format(sys.argv[0]), file=sys.stderr)
    sys.exit(1)


def main(name=None, config_path=None):
    if name not in (None, '__main__'):
        return
    context = SigningContext()
    context.config = get_default_config()
    if config_path is None:
        if len(sys.argv) != 2:
            usage()
        config_path = sys.argv[1]
    context.config.update(load_json(path=config_path))
    if context.config.get('verbose'):
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=log_level
    )
    logging.getLogger("taskcluster").setLevel(logging.WARNING)
    log.debug(context.config)
    loop = asyncio.get_event_loop()
    kwargs = {}
    # TODO can we get the signing servers' CA cert on the scriptworkers?
    # if they're real certs, we can skip this
    if context.config.get('ssl_cert'):
        sslcontext = ssl.create_default_context(cafile=context.config['ssl_cert'])
        kwargs['ssl_context'] = sslcontext
    else:
        kwargs['verify_ssl'] = False
    conn = aiohttp.TCPConnector(**kwargs)
    with aiohttp.ClientSession(connector=conn) as session:
        context.session = session
        try:
            loop.run_until_complete(async_main(context))
        except ScriptWorkerTaskException as exc:
            traceback.print_exc()
            sys.exit(exc.exit_code)
    loop.close()


main(name=__name__)
