import grp
import io
import os
import pwd
import re
import subprocess
import tarfile
import time
import yaml

from contextlib import contextmanager

import utils.constants as const

'''
Shell and docker command parser and invoking of commands
within and outside a docker container
'''
# docker commands
check_images = ['docker', 'images']
pull = ['docker', 'pull']
build = ['docker', 'build']
run = ['docker', 'run', '-td']
check_running = ['docker', 'ps', '-a']
copy = ['docker', 'cp']
execute = ['docker', 'exec']
inspect = ['docker', 'inspect']
stop = ['docker', 'stop']
remove = ['docker', 'rm']
delete = ['docker', 'rmi', '-f']
save = ['docker', 'save']

# docker container names
# TODO: randomly generated image and container names
image = 'tern-image'
tag = str(int(time.time()))
container = 'tern-container'

# base image command library
base_file = 'command_lib/base.yml'
# general snippets in command library
snippet_file = 'command_lib/snippets.yml'
# command library
command_lib = {'base': {}, 'snippets': {}}
with open(os.path.abspath(base_file)) as f:
    command_lib['base'] = yaml.safe_load(f)
with open(os.path.abspath(snippet_file)) as f:
    command_lib['snippets'] = yaml.safe_load(f)


def get_shell_commands(run_comm):
    '''Given a RUN command return a list of shell commands to be run'''
    comm_list = run_comm.split('&&')
    cleaned_list = []
    for comm in comm_list:
        cleaned_list.append(comm.strip())
    return cleaned_list


# from https://stackoverflow.com/questions/6194499/pushd-through-os-system
@contextmanager
def pushd(path):
    curr_path = os.getcwd()
    os.chdir(path)
    yield
    os.chdir(curr_path)


def docker_command(command, *extra):
    '''Invoke docker command. If the command fails nothing is returned
    If it passes then the result is returned'''
    full_cmd = []
    sudo = True
    try:
        members = grp.getgrnam('docker').gr_mem
        if pwd.getpwnam(os.getuid()) in members:
            sudo = False
    except:
        pass
    # TODO: Figure out how to resolve the container when a docker command
    # fails
    if sudo:
        full_cmd.append('sudo')
    full_cmd.extend(command)
    for arg in extra:
        full_cmd.append(arg)
    # invoke
    try:
        print("Running command: " + ' '.join(full_cmd))
        result = subprocess.check_output(full_cmd)
        return result
    except subprocess.CalledProcessError as error:
        print(error)


def parse_command(command):
    '''Typically a unix command is of the form:
        command (subcommand) [options] [arguments]
    Convert a given command into a dictionary of the form:
        {'name': command,
         'subcommand': subcommand,
         'options': [list of options]
         'arguments': [list of arguments]}'''
    options = re.compile('^-')
    options_list = []
    args_list = []
    command_dict = {}
    command_words = command.split(' ')
    # first word is the command name
    command_dict.update({'name': command_words.pop(0), 'subcommand': ''})
    # check if the first word is an option
    first = command_words.pop(0)
    if not options.match(first):
        # this is a subcommand
        command_dict.update({'subcommand': first})
    else:
        # append this to the list of options
        options_list.append(first)
    # find options and arguments in the rest of the list
    while command_words:
        if options.match(command_words[0]):
            options_list.append(command_words.pop(0))
        else:
            args_list.append(command_words.pop(0))
    # now we have options and arguments
    command_dict.update({'options': options_list,
                         'arguments': args_list})
    return command_dict


def check_sourcable(command, package_name):
    '''Given a command and package name find out if the sources can be traced
    back. We find this out by checking the package against the command library
    If the package has a url or source retrieval steps associated with it
    then we return True. If not then we return false'''
    sourcable = False
    if command in command_lib['snippets'].keys():
        for package in command_lib['snippets'][command]['packages']:
            if package['name'] == package_name or \
                    package['name'] == 'default':
                if 'url' in package.keys() or \
                        'src' in package.keys():
                    sourcable = True
    return sourcable


def get_packages(docker_commands):
    '''Given the docker commands in a dockerfile,  get a dictionary of
    packages that are in the command library of retrievable sources
    If it does not exist in the library then record them under
    unrecognized packages
    the dict looks like this:
        recognized:{ <command name>: [list of packages installed with it]}
        unrecognized: {<command name>: [list of packages installed with it]}
    '''
    pkg_dict = {'recognized': {}, 'unrecognized': {}}
    shell_commands = []
    for docker_command in docker_commands:
        if docker_command[0] == 'RUN':
            shell_commands.extend(get_shell_commands(docker_command[1]))
    for command in shell_commands:
        command_obj = parse_command(command)
        if command_obj['name'] in command_lib['snippets'].keys():
            if check_sourcable(command_obj['name']):
                if command_obj['name'] in pkg_dict['recognized'].keys():
                    pkg_dict['recognized'][command_obj['name']].extend(
                        command_obj['arguments'])
                else:
                    pkg_dict['recognized'].update(
                        {command_obj['name']: command_obj['arguments']})
            else:
                if command_obj['name'] in pkg_dict['unrecognized'].keys():
                    pkg_dict['unrecognized'][command_obj['name']].extend(
                        command_obj['arguments'])
                else:
                    pkg_dict['unrecognized'].update(
                        {command_obj['name']: command_obj['arguments']})
    return pkg_dict


def remove_uninstalled(pkg_dict):
    '''Given a dictionary of commands and the packages that got installed
    with it, remove the ones that got uninstalled.
    Currently, there is a happy coincidence that if a package was installed
    and then uninstalled, they occur as duplicates in the list of packages.
    This may not always be true though so this should be something to test'''
    for command in pkg_dict.keys():
        pkg_list = pkg_dict[command]
        remain_list = []
        while pkg_list:
            pkg = pkg_list.pop()
            if pkg in remain_list:
                remain_list.remove(pkg)
            else:
                remain_list.append(pkg)
        pkg_dict[command] = remain_list
    return pkg_dict


def check_container():
    '''Check if a container exists'''
    is_container = False
    keyvalue = 'name=' + container
    result = docker_command(check_running, '--filter', keyvalue)
    result_lines = result.decode('utf-8').split('\n')
    if len(result_lines) > 2:
        is_container = True
    return is_container


def check_image(image_tag_string):
    '''Check if image exists'''
    is_image = False
    result = docker_command(check_images, image_tag_string)
    result_lines = result.decode('utf-8').split('\n')
    if len(result_lines) > 2:
        is_image = True
    return is_image


def build_container(dockerfile, image_tag_string):
    '''Invoke docker command to build a docker image from the dockerfile
    It is assumed that docker is installed and the docker daemon is running'''
    path = os.path.dirname(dockerfile)
    if not check_image(image_tag_string):
        with pushd(path):
            docker_command(build, '-t', image_tag_string, '-f',
                           os.path.basename(dockerfile), '.')


def start_container(image_tag_string):
    '''Invoke docker command to start a container
    If one already exists then stop it
    Use this only in the beginning of running commands within a container
    Assumptions: Docker is installed and the docker daemon is running
    There is no other running container from the given image'''
    if check_container():
        remove_container()
    docker_command(run, '--name', container, image_tag_string)


def remove_container():
    '''Remove a running container'''
    if check_container():
        docker_command(stop, container)
        docker_command(remove, container)


def remove_image(image_tag_string):
    '''Remove an image'''
    if check_image(image_tag_string):
        docker_command(delete, image_tag_string)


def get_base_info(image_tuple):
    '''Given the base image tag tuple, return the info for package retrieval
    snippets'''
    info = ''
    if image_tuple[0] in command_lib['base'].keys():
        if image_tuple[1] in \
                command_lib['base'][image_tuple[0]]['tags'].keys():
            info = \
                command_lib['base'][image_tuple[0]]['tags'][image_tuple[1]]
    return info


def get_latest_tag(base_image):
    '''Given the base image get the latest tag'''
    return command_lib['base'][base_image]['latest']


def invoke_in_container(snippet_list, shell, package=''):
    '''Invoke the commands from the invoke dictionary within a running
    container'''
    for snippet in snippet_list:
        full_cmd = snippet.format(package=package)
        try:
            result = docker_command(execute, container, shell, '-c',
                                    full_cmd)
            # convert from bytestream to string
            try:
                result = result.decode('utf-8')
            except AttributeError:
                pass
        except:
            print("Error executing command inside the container")
    return result


def get_image_id(image_tag_string):
    '''Get the image ID by inspecting the image'''
    result = docker_command(inspect, "-f'{{json .Id}}'", image_tag_string)
    return result.split(':').pop()


def extract_image_metadata(image_tag_string):
    '''Run docker save and extract the files in a temporary directory'''
    success = True
    temp_path = os.path.abspath(const.temp_folder)
    result = docker_command(save, image_tag_string)
    if not result:
        success = False
    else:
        with tarfile.open(fileobj=io.BytesIO(result)) as tar:
            tar.extractall(temp_path)
        if not os.path.exists(temp_path):
            success = False
    return success