#!/usr/bin/env python3
"""
Multi-path Mininet topology for Project 27 (Congestion Detection & Traffic Rerouting).

            +---- s2 ----+
 h1,h2,h4 - s1            s4 - h3, h5
            +---- s3 ----+

  Path A: s1 -> s2 -> s4   (default path, will be congested)
  Path B: s1 -> s3 -> s4   (alternate path)

  h1, h2 : UDP senders (application traffic)
  h3     : UDP receiver (also sends QoS reports to the controller)
  h4, h5 : background-traffic pair used to create controlled congestion

Switch port numbers are fixed so the controller can rely on them:
  s1: 1=h1 2=h2 3=h4 4=s2 5=s3
  s2: 1=s1 2=s4
  s3: 1=s1 2=s4
  s4: 1=h3 2=s2 3=s3 4=h5

Management link (out-of-band, does NOT go through the OpenFlow switches):
  h3-eth1 (192.168.100.3)  <-->  root-eth0 (192.168.100.1, the VM itself)
  The receiver on h3 sends QoS reports over this link to the Ryu controller,
  which runs on the VM and listens on UDP port 9000.

Run (after starting the controller):
  sudo python3 topology.py            # path links default to 10 Mbps
  sudo python3 topology.py --bw 5
"""

import argparse

from mininet.cli import CLI
from mininet.link import Link, TCLink
from mininet.log import setLogLevel, info
from mininet.net import Mininet
from mininet.node import Node, OVSSwitch, RemoteController
from mininet.topo import Topo

MGMT_HOST_IP = '192.168.100.3/24'
MGMT_ROOT_IP = '192.168.100.1/24'


class MultiPathTopo(Topo):
    def build(self, path_bw=10, delay='2ms'):
        hosts = {}
        for i in range(1, 6):
            hosts[i] = self.addHost(f'h{i}', ip=f'10.0.0.{i}/24',
                                    mac=f'00:00:00:00:00:0{i}')

        sw = {}
        for i in range(1, 5):
            sw[i] = self.addSwitch(f's{i}', dpid=f'{i:016x}',
                                   protocols='OpenFlow13')

        # Host links are not rate-limited; congestion is created on the paths.
        self.addLink(hosts[1], sw[1], port2=1)
        self.addLink(hosts[2], sw[1], port2=2)
        self.addLink(hosts[4], sw[1], port2=3)
        self.addLink(hosts[3], sw[4], port2=1)
        self.addLink(hosts[5], sw[4], port2=4)

        path_link = dict(bw=path_bw, delay=delay)

        # Path A: s1 - s2 - s4
        self.addLink(sw[1], sw[2], port1=4, port2=1, **path_link)
        self.addLink(sw[2], sw[4], port1=2, port2=2, **path_link)

        # Path B: s1 - s3 - s4
        self.addLink(sw[1], sw[3], port1=5, port2=1, **path_link)
        self.addLink(sw[3], sw[4], port1=2, port2=3, **path_link)


def add_management_link(net):
    """Connect h3 directly to the VM (root namespace) so it can reach Ryu."""
    root = Node('root', inNamespace=False)
    h3 = net.get('h3')
    link = net.addLink(h3, root, cls=Link)
    h3.setIP(MGMT_HOST_IP, intf=link.intf1)
    root.setIP(MGMT_ROOT_IP, intf=link.intf2)
    return root


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bw', type=float, default=10,
                        help='bandwidth of each path link in Mbps (default 10)')
    parser.add_argument('--controller-ip', default='127.0.0.1')
    parser.add_argument('--controller-port', type=int, default=6653)
    args = parser.parse_args()

    setLogLevel('info')
    net = Mininet(topo=MultiPathTopo(path_bw=args.bw),
                  switch=OVSSwitch, link=TCLink,
                  controller=None, autoSetMacs=False)
    net.addController('c0', controller=RemoteController,
                      ip=args.controller_ip, port=args.controller_port)
    add_management_link(net)
    net.start()

    # Static ARP entries: the network has a loop, so we avoid ARP broadcasts.
    net.staticArp()

    info('\n*** Topology up.')
    info('\n*** QoS reports: h3 -> 192.168.100.1:9000 (management link)')
    info('\n*** Try: pingall, h3 ping -c 2 192.168.100.1, xterm h1 h2 h3\n')
    CLI(net)
    net.stop()


if __name__ == '__main__':
    main()
