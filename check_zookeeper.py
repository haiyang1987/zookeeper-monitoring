#! /usr/bin/env python
""" Check Zookeeper Cluster

Generic monitoring script that could be used with multiple platforms.

It requires ZooKeeper 3.4.0 or greater or patch ZOOKEEPER-744
"""

import sys
import socket
import logging
import re
import subprocess

from StringIO import StringIO
from optparse import OptionParser, OptionGroup

__version__ = (0, 1, 0)

log = logging.getLogger()
logging.basicConfig(level=logging.DEBUG)

class NagiosHandler(object):

    @classmethod
    def register_options(cls, parser):
        group = OptionGroup(parser, 'Nagios specific options')

        group.add_option('-w', '--warning', dest='warning')
        group.add_option('-c', '--critical', dest='critical')

        parser.add_option_group(group)

    def analyze(self, opts, cluster_stats):
        try:
            warning = int(opts.warning)
            critical = int(opts.critical)

        except (TypeError, ValueError):
            print >>sys.stderr, 'Invalid values for "warning" and "critical".'
            return 2

        if opts.key is None:
            print >>sys.stderr, 'You should specify a key name.'
            return 2

        warning_state, critical_state, values = [], [], []
        for host, stats in cluster_stats.items():
            if opts.key in stats:

                value = stats[opts.key]
                values.append('%s=%s;%s;%s' % (host, value, warning, critical))

                if warning >= value > critical or warning <= value < critical:
                    warning_state.append(host)

                elif (warning < critical and critical <= value) or (warning > critical and critical >= value):
                    critical_state.append(host)

        values = ' '.join(values)
        if critical_state:
            print 'Critical "%s" %s!|%s' % (opts.key, ', '.join(critical_state), values)
            return 2
        
        elif warning_state:
            print 'Warning "%s" %s!|%s' % (opts.key, ', '.join(warning_state), values)
            return 1

        else:
            print 'Ok "%s"!|%s' % (opts.key, values)
            return 0

class CactiHandler(object):

    @classmethod
    def register_options(cls, parser):
        group = OptionGroup(parser, 'Cacti specific options')
        
        group.add_option('-l', '--leader', dest='leader', 
            action="store_true", help="only query the cluster leader")

        parser.add_option_group(group)

    def analyze(self, opts, cluster_stats):
        if opts.key is None:
            print >>sys.stderr, 'The key name is mandatory.'
            return 1

        if opts.leader is True:
            try:
                leader = [x for x in cluster_stats.values() \
                    if x.get('zk_server_state', '') == 'leader'][0] 

            except IndexError:
                print >>sys.stderr, 'No leader found.'
                return 3

            if opts.key in leader:
                print leader[opts.key]
                return 0

            else:
                print >>sys.stderr, 'Unknown key: "%s"' % opts.key
                return 2
        else:
            for host, stats in cluster_stats.items():
                if opts.key not in stats: 
                    continue

                host = host.replace(':', '_')
                print '%s:%s' % (host, stats[opts.key]),


class GangliaHandler(object):

    gmetric = '/usr/bin/gmetric'

    @classmethod
    def register_options(cls, parser):
        pass

    def call(self, *args, **kwargs):
        subprocess.call(*args, **kwargs)

    def analyze(self, opts, cluster_stats):
        if len(cluster_stats) != 1:
            print >>sys.stderr, 'Only allowed to monitor a single node.'
            return 1

        for host, stats in cluster_stats.items():
            for k, v in stats.items():
                try:
                    self.call([self.gmetric, '-n', k, '-v', str(int(v)), '-t', 'uint32'])
                except (TypeError, ValueError):
                    pass

class ZooKeeperServer(object):

    def __init__(self, host='localhost', port='2181', timeout=1):
        self._address = (host, int(port))
        self._timeout = timeout

    def get_stats(self):
        """ Get ZooKeeper server stats as a map """
        data = self._send_cmd('mntr')
        if data:
            return self._parse(data)
        else:
            data = self._send_cmd('stat')
            return self._parse_stat(data)

    def _create_socket(self):
        return socket.socket()

    def _send_cmd(self, cmd):
        """ Send a 4letter word command to the server """
        s = self._create_socket()
        s.settimeout(self._timeout)

        s.connect(self._address)
        s.send(cmd)

        data = s.recv(2048)
        s.close()

        return data

    def _parse(self, data):
        """ Parse the output from the 'mntr' 4letter word command """
        h = StringIO(data)
        
        result = {}
        for line in h.readlines():
            try:
                key, value = self._parse_line(line)
                result[key] = value
            except ValueError:
                pass # ignore broken lines

        return result

    def _parse_stat(self, data):
        """ Parse the output from the 'stat' 4letter word command """
        h = StringIO(data)

        result = {}
        
        version = h.readline()
        if version:
            result['zk_version'] = version[version.index(':')+1:].strip()

        # skip all lines until we find the empty one
        while h.readline().strip(): pass

        for line in h.readlines():
            m = re.match('Latency min/avg/max: (\d+)/(\d+)/(\d+)', line)
            if m is not None:
                result['zk_min_latency'] = int(m.group(1))
                result['zk_avg_latency'] = int(m.group(2))
                result['zk_max_latency'] = int(m.group(3))
                continue

            m = re.match('Received: (\d+)', line)
            if m is not None:
                result['zk_packets_received'] = int(m.group(1))
                continue

            m = re.match('Sent: (\d+)', line)
            if m is not None:
                result['zk_packets_sent'] = int(m.group(1))
                continue

            m = re.match('Outstanding: (\d+)', line)
            if m is not None:
                result['zk_outstanding_requests'] = int(m.group(1))
                continue

            m = re.match('Mode: (.*)', line)
            if m is not None:
                result['zk_server_state'] = m.group(1)
                continue

            m = re.match('Node count: (\d+)', line)
            if m is not None:
                result['zk_znode_count'] = int(m.group(1))
                continue

        return result 

    def _parse_line(self, line):
        try:
            key, value = map(str.strip, line.split('\t'))
        except ValueError:
            raise ValueError('Found invalid line: %s' % line)

        if not key:
            raise ValueError('The key is mandatory and should not be empty')

        try:
            value = int(value)
        except (TypeError, ValueError):
            pass

        return key, value

def main():
    opts, args = parse_cli()

    cluster_stats = get_cluster_stats(opts.servers)
    if opts.output is None:
        dump_stats(cluster_stats)
        return 0

    handler = create_handler(opts.output)
    if handler is None:
        log.error('undefined handler: %s' % opts.output)
        sys.exit(1)

    return handler.analyze(opts, cluster_stats)

def create_handler(name):
    """ Return an instance of a platform specific analyzer """
    try:
        return globals()['%sHandler' % name.capitalize()]()
    except KeyError:
        return None

def get_all_handlers():
    """ Get a list containing all the platform specific analyzers """
    return [NagiosHandler, CactiHandler, GangliaHandler]

def dump_stats(cluster_stats):
    """ Dump cluster statistics in an user friendly format """
    for server, stats in cluster_stats.items():
        print 'Server:', server

        for key, value in stats.items():
            print "%30s" % key, ' ', value
        print

def get_cluster_stats(servers):
    """ Get stats for all the servers in the cluster """
    stats = {}
    for host, port in servers:
        try:
            zk = ZooKeeperServer(host, port)
            stats["%s:%s" % (host, port)] = zk.get_stats()

        except socket.error, e:
            # ignore because the cluster can still work even 
            # if some servers fail completely

            # this error should be also visible in a variable
            # exposed by the server in the statistics

            logging.info('unable to connect to server '\
                '"%s" on port "%s"' % (host, port))

    return stats


def get_version():
    return '.'.join(map(str, __version__))


def parse_cli():
    parser = OptionParser(usage='./check_zookeeper.py <options>', version=get_version())

    parser.add_option('-s', '--servers', dest='servers', 
        help='a list of SERVERS', metavar='SERVERS')

    parser.add_option('-o', '--output', dest='output', 
        help='output HANDLER: nagios, ganglia, cacti', metavar='HANDLER')

    parser.add_option('-k', '--key', dest='key')

    for handler in get_all_handlers():
        handler.register_options(parser)

    opts, args = parser.parse_args()

    if opts.servers is None:
        parser.error('The list of servers is mandatory')

    opts.servers = [s.split(':') for s in opts.servers.split(',')]

    return (opts, args)


if __name__ == '__main__':
    sys.exit(main())

