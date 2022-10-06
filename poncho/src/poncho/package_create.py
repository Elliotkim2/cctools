#!/usr/bin/env python3

# Copyright (C) 2022 The University of Notre Dame
# This software is distributed under the GNU General Public License.
# See the file COPYING for details.

import os
import sys
import tempfile
import argparse
import subprocess
import json
import conda_pack
import pathlib
import hashlib
import shutil
import logging
import re
from packaging import version

logger = logging.getLogger()
logging.basicConfig(level=logging.INFO, format='%(asctime)s:%(levelname)s:%(message)s')


def pack_env(spec, output):

    # record packages installed as editable from pip
    local_pip_pkgs = _find_local_pip()

    with tempfile.TemporaryDirectory() as env_dir:
        logger.info('Creating temporary environment in {}'.format(env_dir))

        # creates conda spec file from poncho spec file
        logger.info('Converting spec file...')
        conda_spec = create_conda_spec(spec, env_dir, local_pip_pkgs)

        # fetch data via git and https
        logger.info('Fetching git data...')
        git_data(spec, env_dir)

        logger.info('Fetching http data...')
        http_data(spec, env_dir)

        # create conda environment in temp directory
        logger.info('Populating environment...')
        _run_conda_command(env_dir, 'env create', '--file', env_dir + '/conda_spec.yml')

        logger.info('Adding local packages...')
        for (name, path) in conda_spec['pip_local'].items():
            _install_local_pip(env_dir, name, path)

        logger.info('Generating environment file...')

        # Bug breaks bundling common packages (e.g. python).
        # ignore_missing_files may be safe to remove in the future.
        # https://github.com/conda/conda-pack/issues/145
        conda_pack.pack(prefix=env_dir, output=str(output), force=True, ignore_missing_files=True)

        logger.info('To activate environment run poncho_package_run -e {} <command>'.format(output))

    return output


def _run_conda_command(environment, command, *args):
    all_args = ['conda'] + command.split()
    all_args = all_args + ['--prefix={}'.format(str(environment))] + list(args)

    try:
        subprocess.check_output(all_args)
    except subprocess.CalledProcessError as e:
        logger.warning("Error executing: {}".format(' '.join(all_args)))
        print(e.output.decode())
        sys.exit(1)


def _find_local_pip():
    edit_raw = subprocess.check_output([sys.executable, '-m' 'pip', 'list', '--editable']).decode()

    # drop first two lines, which are just a header
    edit_raw = edit_raw.split('\n')[2:]

    path_of = {}
    for line in edit_raw:
        if not line:
            # skip empty lines
            continue
        # we are only interested in the path information of the package, which
        # is in the last column
        (pkg, version, location) = line.split()
        path_of[pkg] = location
    return path_of


def git_data(spec, out_dir):
    f = open(spec, 'r')
    data = json.load(f)

    if 'git' in data:
        for git_dir in data['git']:

            git_repo = None
            ref = None

            if 'remote' in data['git'][git_dir]:
                git_repo = data['git'][git_dir]['remote']
            if 'ref' in data['git'][git_dir]:
                ref = data['git'][git_dir]['ref']

            if git_repo:
                # clone repo
                path = '{}/{}'.format(out_dir, git_dir)

                subprocess.check_call(['git', 'clone', git_repo, path])

                if not os.path.exists(out_dir + '/poncho'):
                    os.mkdir(out_dir + '/poncho')

                # add to script
                gd = 'export {}=$1/{}\n'.format(git_dir, git_dir)
                with open(out_dir + '/poncho/set_env', 'a') as f:
                    f.write(gd)


def _install_local_pip(env_dir, pip_name, pip_path):
    logger.info("Installing {} from editable pip".format(pip_path))
    # TODO GET pip version
    pip_exec = shutil.which('pip')
    process = subprocess.Popen([pip_exec, '-V'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = process.communicate()
    pip_version = out.decode('utf-8').split()[1]
    if version.parse(pip_version) < version.parse('22.1'):
        _run_conda_command(env_dir, 'run', 'pip', 'install', '--use-feature=in-tree-build', pip_path)
    else:
        _run_conda_command(env_dir, 'run', 'pip', 'install', pip_path)


def http_data(spec, out_dir):
    f = open(spec, 'r')
    data = json.load(f)

    if 'http' in data:
        for filename in data['http']:

            file_type = None
            compression = None
            url = None

            if 'type' in data['http'][filename]:
                file_type = data['http'][filename]['type']
            if 'compression' in data['http'][filename]:
                compression = data['http'][filename]['compression']
            if 'url' in data['http'][filename]:
                url = data['http'][filename]['url']

            if url:
                # curl datai
                path = '{}/{}'.format(out_dir, filename)

                if file_type == 'tar' and compression == 'gzip':
                    tgz = path + '.tar.gz'
                    subprocess.check_call(['curl', url, '--output', tgz])
                    os.mkdir(path)
                    subprocess.check_call(['tar', '-xzf', tgz, '-C', path])

                elif file_type == 'tar':
                    tar = path + '.tar'
                    subprocess.check_call(['curl', url, '--output', tar])
                    os.mkdir(path)
                    subprocess.check_call(['tar', '-xf', tar, '-C', path])

                elif compression == 'gzip':
                    gz = path + '.gz'
                    subprocess.check_call(['curl', url, '--output', gz])
                    subprocess.check_call(['gzip', '-d', gz])

                else:
                    subprocess.check_call(['curl', url, '--output', path])
                if not os.path.exists(out_dir + '/poncho'):
                    os.mkdir(out_dir + '/poncho')

                gd = 'export {}=$1/{}\n'.format(filename, filename)
                with open(out_dir + '/poncho/set_env', 'a') as f:
                    f.write(gd)


def create_conda_spec(spec_file, out_dir, local_pip_pkgs):
    f = open(spec_file, 'r')
    poncho_spec = json.load(f)

    conda_spec = {}
    conda_spec['channels'] = []
    conda_spec['dependencies'] = set()
    conda_spec['name'] = 'base'

    # packages in the spec that are installed in the current environment with
    # pip --editable
    local_reqs = set()
    
    if 'conda' in poncho_spec:

        conda_spec['channels'] = poncho_spec['conda'].get('channels', ['conda-forge', 'defaults'])

        if 'dependencies' in poncho_spec['conda']:

            conda_spec['dependencies'] = poncho_spec['conda'].get('dependencies', [])

            for dep in list(conda_spec['dependencies']):
                if isinstance(dep, dict) and 'pip' in dep:
                    for pip_dep in list(dep['pip']):
                        only_name = re.sub("[!~=<>].*$", "", pip_dep)  # remove possible version from spec
                        if only_name in local_pip_pkgs:
                            local_reqs.add(only_name)
                            dep['pip'].remove(pip_dep)
                else:
                    only_name = re.sub("[!~=<>].*$", "", dep)  # remove possible version from spec
                    if only_name in local_pip_pkgs:
                        local_reqs.add(only_name)
                        conda_spec['dependencies'].remove(dep)

            conda_spec['dependencies'] = list(conda_spec['dependencies'])
        
        # OLD FORMAT
        else:

            conda_spec['dependencies'] = set(poncho_spec['conda'].get('packages', []))

            for dep in list(conda_spec['dependencies']):
                only_name = re.sub("[!~=<>].*$", "", dep)  # remove possible version from spec
                if only_name in local_pip_pkgs:
                    local_reqs.add(only_name)
                    conda_spec['dependencies'].remove(dep)
            conda_spec['dependencies'] = list(conda_spec['dependencies'])


            pip_pkgs = set(poncho_spec.get('pip', []))
            
            for dep in list(pip_pkgs):
                only_name = re.sub("[!~=<>].*$", "", dep)  # remove possible version from spec
                if only_name in local_pip_pkgs:
                    local_reqs.add(only_name)
                    pip_pkgs.remove(dep)

            conda_spec['dependencies'].append({'pip': list(pip_pkgs)})


    for (pip_name, location) in local_pip_pkgs.items():
        if pip_name not in local_reqs:
            logger.warning("pip package {} was found as pip --editable, but it is not part of the spec. Ignoring local installation.".format(pip_name))

    with open(out_dir + '/conda_spec.yml',  'w') as jf:
        json.dump(conda_spec, jf, indent=4)

    # adding local pips to the spec after writing file, as conda complains of
    # unknown field.
    conda_spec['pip_local'] = {name:local_pip_pkgs[name] for name in local_reqs}

    return conda_spec
