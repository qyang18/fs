#!/usr/bin/python

__author__ = 'jsommers@colgate.edu'

import pydot
from node import *
from link import *
import traffic
from pytricia import PyTricia
import ipaddr
import fscommon
import fsutil
import json

from networkx import single_source_dijkstra_path, single_source_dijkstra_path_length, read_gml, read_dot
from networkx.readwrite import json_graph


class InvalidTrafficSpecification(Exception):
    pass

class InvalidRoutingConfiguration(Exception):
    pass

class NullTopology(object):
    def start(self):
        pass

    def stop(self):
        pass

class Topology(NullTopology):
    def __init__(self, graph, nodes, links, traffic_modulators, debug=False):
        self.logger = fscommon.get_logger()
        self.debug = debug
        self.graph = graph
        self.nodes = nodes
        self.links = links
        self.traffic_modulators = traffic_modulators
        self.routing = {}
        self.ipdestlpm = PyTricia()
        self.owdhash = {}
        self.__configure_routing()

        for a,b,d in self.graph.edges(data=True):
            if 'reliability' in d:
                self.__configure_edge_reliability(a,b,d['reliability'],d)


    def __configure_edge_reliability(self, a, b, relistr, edict):
        relidict = fsutil.mkdict(relistr)
        ttf = ttr = None
        for k,v in relidict.iteritems():
            if k == 'failureafter':
                ttf = eval(v)
                if isinstance(ttf, (int, float)):
                    ttf = modulation_generator([ttf])

            elif k == 'downfor':
                ttr = eval(v)
                if isinstance(ttr, (int, float)):
                    ttr = modulation_generator([ttr])

            elif k == 'mttf':
                ttf = eval(v)

            elif k == 'mttr':
                ttr = eval(v)

        if ttf or ttr:
            assert(ttf and ttr)
            xttf = next(ttf)
            self.after(xttf, 'link-failure-'+a+'-'+b, self.__linkdown, a, b, edict, ttf, ttr)

    def __configure_routing(self):
        for n in self.graph:
            self.routing[n] = single_source_dijkstra_path(self.graph, n)
        if self.debug:
            for n,d in self.graph.nodes_iter(data=True):
                print n,d

        self.ipdestlpm = PyTricia()
        for n,d in self.graph.nodes_iter(data=True):
            dlist = d.get('ipdests','').split()
            if self.debug:
                print dlist,n
            for destipstr in dlist:
                ipnet = ipaddr.IPNetwork(destipstr)
                xnode = {}
                self.ipdestlpm[str(ipnet)] = xnode
                if 'dests' in xnode:
                    xnode['dests'].append(n)
                else:
                    xnode['net'] = ipnet
                    xnode['dests'] = [ n ]

        self.owdhash = {}
        for a in self.graph:
            for b in self.graph:
                key = a + ':' + b
                
                rlist = [ a ]
                while rlist[-1] != b:
                    nh = self.nexthop(rlist[-1], b)
                    if not nh:
                        self.logger.debug('No route from %s to %s (in owd; ignoring)' % (a,b))
                        return None
                    rlist.append(nh)

                owd = 0.0
                for i in xrange(len(rlist)-1):
                    owd += self.delay(rlist[i],rlist[i+1])
                self.owdhash[key] = owd

    def node(self, nname):
        '''get the node object corresponding to a name '''
        return self.nodes[nname]

    def start(self):
        for tm in self.traffic_modulators:
            tm.start()

        for nname,n in self.nodes.iteritems():
            n.start()

    def stop(self):
        for nname,n in self.nodes.iteritems():
            n.stop()     
            
    def __linkdown(self, a, b, edict, ttf, ttr):
        '''kill a link & recompute routing '''
        self.logger.info('Link failed %s - %s' % (a,b))
        self.graph.remove_edge(a,b)
        self.__configure_routing()

        uptime = None
        try:
            uptime = next(ttr)
        except:
            self.logger.info('Link %s-%s permanently taken down (no recovery time remains in generator)' % (a, b))
            return
        else:
            self.after(uptime, 'link-recovery-'+a+'-'+b, self.__linkup, a, b, edict, ttf, ttr)

        
    def __linkup(self, a, b, edict, ttf, ttr):
        '''revive a link & recompute routing '''
        self.logger.info('Link recovered %s - %s' % (a,b))
        self.graph.add_edge(a,b,weight=edict.get('weight',1),delay=edict.get('delay',0),capacity=edict.get('capacity',1000000))
        FsConfigurator.configure_routing(self.routing, self.graph)

        downtime = None
        try:
            downtime = next(ttf)
        except:
            self.logger.info('Link %s-%s permanently going into service (no failure time remains in generator)' % (a, b))
            return
        else:
            self.after(downtime, 'link-failure-'+a+'-'+b, self.__linkdown, a, b, edict, ttf, ttr)


    def owd(self, a, b):
        '''get the raw one-way delay between a and b '''
        key = a + ':' + b
        rv = None
        if key in self.owdhash:
            rv = self.owdhash[key]
        return rv


    def delay(self, a, b):
        '''get the link delay between a and b '''
        d = self.graph[a][b]
        if d and 0 in d:
            return float(d[0]['delay'])
        return None

        
    def capacity(self, a, b):
        '''get the bandwidth between a and b '''
        d = self.graph[a][b]
        if d and 0 in d:
            return int(d[0]['capacity'])
        return None
        
    def nexthop(self, node, dest):
        '''
        return the next hop node for a given destination.
        node: current node
        dest: dest node name
        returns: next hop node name
        '''
        try:
            nlist = self.routing[node][dest]
        except:
            return None
        if len(nlist) == 1:
            return nlist[0]
        return nlist[1]

    def destnode(self, node, dest):
        '''
        return the destination node corresponding to a dest ip.
        node: current node
        dest: ipdest
        returns: destination node name
        '''
        # radix trie lpm lookup for destination IP prefix
        xnode = self.ipdestlpm.get(dest, None)

        if xnode:
            dlist = xnode['dests']
            best = None
            if len(dlist) > 1: 
                # in the case that there are multiple egress nodes
                # for the same IP destination, choose the closest egress
                best = None
                bestw = 10e6
                for d in dlist:
                    w = single_source_dijkstra_path_length(self.graph, node, d)
                    if w < bestw:
                        bestw = w
                        best = d
            else:
                best = dlist[0]

            return best
        else:
            raise InvalidRoutingConfiguration('No route for ' + dest)


class FsConfigurator(object):
    def __init__(self, debug):
        self.debug = debug
        self.logger = fscommon.get_logger(debug)

    def __strip_strings(self):
        '''Clean up all the strings in the imported config.'''
        for k in self.graph.graph['graph']:
            if isinstance(self.graph.graph['graph'][k], (str,unicode)):
                v = self.graph.graph['graph'][k].replace('"','').strip()
                self.graph.graph['graph'][k] = v

        for n,d in self.graph.nodes(data=True):
            for k in d:
                if isinstance(d[k], (str,unicode)):
                    v = d[k].replace('"','').strip()
                    d[k] = v

        for a,b,d in self.graph.edges(data=True):
            for k in d:
                if isinstance(d[k], (str,unicode)):
                    v = d[k].replace('"','').strip()
                    d[k] = v

    def __substitute(self, val):
        '''Recursively substitute $identifiers in a config string'''
        if not isinstance(val, (str,unicode)):
            return val

        # if $identifier (minus $) is a key in graph, replace
        # it with value of that key.  then fall-through and
        # recursively substitute any $identifiers
        if val in self.graph.graph['graph']:
            self.logger.debug("Found substitution for {}: {}".format(val, self.graph.graph['graph'][val]))
            val = self.graph.graph['graph'][val]

            # if the resolved value isn't a string, no possible way to do further substitutions, BUT
            # still need to return as a string to make any higher-up joins work correctly.  ugh.
            if not isinstance(val, (str,unicode)):
                return str(val)

        items = val.split()
        for i in range(len(items)):
            if items[i][0] == '$':
                # need to do a substitution
                self.logger.debug("Found substitution symbol {} -- recursing".format(items[i]))
                items[i] = self.__substitute(items[i][1:])
        return ' '.join(items)

    def __do_substitutions(self):
        '''For every string value in graph, nodes, and links, find any $identifier
           and do a (recursive) substitution of strings, essentially in place (use split/join 
           to effectively do that.'''
        for k in self.graph.graph['graph']:
            v = self.graph.graph['graph'][k]
            self.graph.graph['graph'][k] = self.__substitute(v)

        for n,d in self.graph.nodes(data=True):
            for k,v in d.iteritems():
                d[k] = self.__substitute(v)

        for a,b,d in self.graph.edges(data=True):
            for k,v in d.iteritems():
                d[k] = self.__substitute(v)

    def load_config(self, config, configtype="json"):
        try:
            if configtype == "dot":
                self.graph = read_dot(config)
            elif configtype == "json":
                self.graph = json_graph.node_link_graph(json.loads(open(config).read().strip()))
            elif configtype == "gml":
                self.graph = read_gml(config)
        except Exception,e:
            print "Config read error: {}".format(str(e))
            self.logger.error("Error reading configuration: {}".format(str(e)))
            sys.exit(-1)
         
        mconfig_dict = {'counterexport':False, 'flowexportfn':'null_export_factory','counterexportinterval':0, 'counterexportfile':None, 'maintenance_cycle':60, 'pktsampling':1.0, 'flowsampling':1.0, 'longflowtmo':-1, 'flowinactivetmo':-1}

        print "Reading config for graph {}.".format(self.graph.graph.get('name','(unnamed)'))

        self.__strip_strings()
        self.__do_substitutions()

        measurement_nodes = self.graph.nodes()
        for key in self.graph.graph['graph']:
            val = self.graph.graph['graph'][key]
            mconfig_dict[key] = val
            if key in ['measurenodes','measurementnodes','measurements']:
                if val != 'all':
                    measurement_nodes = [ n.strip() for n in val.split() ]

        measurement_config = MeasurementConfig(**mconfig_dict)
        print "Running measurements on these nodes: <{}>".format(','.join(measurement_nodes))

        for a,b,d in self.graph.edges(data=True):
            w = 1
            if 'weight' in d:
                w = d['weight']
            d['weight'] = int(w)

        self.nodes = {}
        self.links = {}
        self.traffic_modulators = []

        self.__configure_parallel_universe(measurement_config, measurement_nodes)
        self.__configure_traffic()
        if self.debug:
            self.__print_config()
        return Topology(self.graph, self.nodes, self.links, self.traffic_modulators, debug=self.debug)

    def __print_config(self):
        print "*** Begin Configuration Dump ***".center(30)
        print "*** nodes ***"
        for n,d in self.graph.nodes(data=True):
            print n,d
        print "*** links ***"
        for a,b,d in self.graph.edges(data=True):
            print a,b,d
        print "*** End Configuration Dump ***".center(30)

    def __addupd_router(self, rname, rdict, measurement_config):
        robj = None
        forwarding = None
        typehash = {'iprouter':Router, 'ofswitch':OpenflowSwitch, 'ofcontroller':OpenflowController}
        if rname not in self.nodes:
            aa = False
            if 'autoack' in rdict:
                aa = rdict['autoack']
                if isinstance(aa, (str,unicode)):
                    aa = eval(aa)
            classtype = rdict.get('type','iprouter')
            # Checking if controller then find out the forwarding technique to be used
            forwarding=None
            if classtype == 'ofcontroller':
                forwarding = rdict.get('forwarding')

            if self.debug:
                self.logger.debug('Adding router {}, {}, autoack={}'.format(rname,rdict,aa))

            if classtype not in typehash:
                raise InvalidTrafficSpecification('Unrecognized node type {}.'.format(classtype))
            robj = typehash[classtype](rname, measurement_config, autoack=aa, forwarding=forwarding)
            self.nodes[rname] = robj
        else:
            robj = self.nodes[rname]
        return robj


    def __configure_parallel_universe(self, measurement_config, measurement_nodes):
        '''
        using the the networkx graph stored in the simulator,
        build corresponding Routers and Links in the sim world.
        '''
        for rname,rdict in self.graph.nodes_iter(data=True):
            self.logger.debug("Adding node {} with data {}".format(rname, rdict))
            mc = measurement_config                
            if rname not in measurement_nodes:
                mc = None
            self.__addupd_router(rname, rdict, mc)

        for a,b,d in self.graph.edges_iter(data=True):
            self.logger.debug("Adding bidirectional link from {}-{} with data {}".format(a, b, d))

            mc = measurement_config                
            if a not in measurement_nodes:
                mc = None
            ra = self.__addupd_router(a, d, mc)

            mc = measurement_config                
            if b not in measurement_nodes:
                mc = None
            rb = self.__addupd_router(b, d, mc)
            
            delay = float(self.graph[a][b][0].get('delay',0))
            cap = float(self.graph[a][b][0].get('capacity',0))

            linkfwd = Link(cap/8, delay, ra, rb)
            linkrev = Link(cap/8, delay, rb, ra)
            aport = ra.add_link(linkfwd, b)
            bport = rb.add_link(linkrev, a)
            self.links[(a,aport,b,bport)] = linkfwd
            self.links[(b,bport,a,aport)] = linkrev
            linkfwd.set_ingress_port(aport)
            linkfwd.set_egress_port(bport)
            linkrev.set_ingress_port(bport)
            linkrev.set_egress_port(aport)

    def __configure_traffic(self):
        for n,d in self.graph.nodes_iter(data=True):
            if 'traffic' not in d:
                continue
                
            modulators = d['traffic'].split()
            self.logger.debug("Traffic modulators configured: {}".format(str(modulators)))

            for mkey in modulators:
                modspecstr = d[mkey]

                self.logger.debug('Configing modulator: {}'.format(str(modspecstr)))
                m = self.__configure_traf_modulator(modspecstr, n, d)
                self.traffic_modulators.append(m)


    def __configure_traf_modulator(self, modstr, srcnode, xdict):
        modspeclist = modstr.split()
        moddict = {}
        for i in xrange(1,len(modspeclist)):
            k,v = modspeclist[i].split('=')
            moddict[k] = v

        self.logger.debug("inside config_traf_mod: {}".format(moddict))
        if not 'profile' in moddict or 'sustain' in moddict:
            self.logger.warn("Need a 'profile' or 'sustain' in traffic specification for {}".format(moddict))
            raise InvalidTrafficSpecification(moddict)

        trafprofname = moddict.get('generator', None)
        st = moddict.get('start', None)
        st = eval(st)
        if isinstance(st, (int, float)):
            st = traffic.randomchoice(st)

        profile = moddict.get('profile', None)
        if not profile:
            profile = moddict.get('sustain', None)

        emerge = moddict.get('emerge', None)
        withdraw = moddict.get('withdraw', None)

        trafprocstr = ""
        if trafprofname in xdict:
            trafprofstr = xdict[trafprofname]
        elif trafprofname in self.graph.graph['graph']:
            trafprofstr = self.graph.graph['graph'][trafprofname]
        else:
            self.logger.warn("Need a traffic generator name ('generator') in {}".format(moddict))
            raise InvalidTrafficSpecification(xdict)

        self.logger.debug("Found traffic specification for {}: {}".format(trafprofname,trafprofstr))
        tgen = self.__configure_traf_spec(trafprofname, trafprofstr, srcnode)
        fm = traffic.FlowEventGenModulator(tgen, stime=st, emerge_profile=emerge, sustain_profile=profile, withdraw_profile=withdraw)
        return fm

     
    def __configure_traf_spec(self, trafname, trafspec, srcnode):
        '''Configure a traffic generator based on specification elements'''
        trafspeclist = trafspec.split()

        # first item in the trafspec list should be the traffic generator name.
        # also need to traverse the remainder of the and do substitutions for common configuration elements
        tclass = trafspeclist[0].strip().lower().capitalize()
        fulltrafspec = trafspeclist[1:]
        trafgenname = "{}TrafficGenerator".format(tclass)

        if trafgenname not in dir(traffic):
            self.logger.warn("Bad config: can't find TrafficGenerator class named {0}.  Add the class '{0}' to traffic.py, or fix the config.".format(trafgenname))
            raise InvalidTrafficSpecification(trafspec)
        else:
            classobj = eval("traffic.{}".format(trafgenname))
            trafdict = fsutil.mkdict(fulltrafspec)
            self.logger.debug("Creating {} with specification {}".format(str(classobj),trafdict))
            gen = lambda: classobj(srcnode, **trafdict)
            return gen
