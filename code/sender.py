"""A Sender for the GBN protocol."""

# Disable pylint rules which are incompatible with our naming conventions
# pylint: disable=C0103,W0221,W0201,R0902,R0913,R0201

import argparse
from os import uname
import queue as que
import logging
from scapy.sendrecv import send
from scapy.config import conf
from scapy.layers.inet import IP, ICMP
from scapy.packet import Packet, bind_layers
from scapy.fields import (BitEnumField, BitField, ShortField, ByteField,
                          ConditionalField)
from scapy.automaton import Automaton, ATMT
import time

FORMAT = "[SENDER:%(lineno)3s - %(funcName)10s()] %(message)s"
logging.basicConfig(format=FORMAT)
log = logging.getLogger('sender')
log.setLevel(logging.DEBUG)
logging.getLogger("scapy.runtime").setLevel(logging.ERROR)

TIMEOUT = 1  # number of seconds before packets are retransmitted


class GBN(Packet):
    """The GBN Header.

    It includes the following fields:
        type: DATA or ACK
        options: sack support
        len: payload length
        hlen: header length
        num: sequence/ACK number
        win: sender/receiver window size
    """
    name = 'GBN'
    fields_desc = [BitEnumField("type", 0, 1, {0: "data", 1: "ack"}),
                   BitField("options", 0, 7),
                   ShortField("len", None),
                   ByteField("hlen", 0),
                   ByteField("num", 0),
                   ByteField("win", 0),
                   ConditionalField(ByteField("block_len",0), lambda pkt:pkt.hlen>6),
                   ConditionalField(ByteField("left_1",0), lambda pkt:pkt.hlen>6),
                   ConditionalField(ByteField("len_1",0), lambda pkt:pkt.hlen>6),
                   ConditionalField(ByteField("pad1",0), lambda pkt:pkt.hlen>9),
                   ConditionalField(ByteField("left_2",0), lambda pkt:pkt.hlen>9),
                   ConditionalField(ByteField("len_2",0), lambda pkt:pkt.hlen>9),
                   ConditionalField(ByteField("pad2",0), lambda pkt:pkt.hlen>12),
                   ConditionalField(ByteField("left_3",0), lambda pkt:pkt.hlen>12),
                   ConditionalField(ByteField("len_3",0), lambda pkt:pkt.hlen>12)]

# GBN header is coming after the IP header
bind_layers(IP, GBN, frag=0, proto=222)


class GBNSender(Automaton):
    """Sender implementation for the GBN protocol using a Scapy automaton.

    Attributes:
        win: Maximum window size of the sender
        n_bits: number of bits used to encode sequence number
        receiver: IP address of the receiver
        sender: IP address of the sender
        q: Queue for all payload messages
        buffer: buffer to save sent but not acknowledged segments
        current: Sequence number of next data packet to send
        unack: First unacked segment
        receiver_win: Current window advertised by receiver, initialized with
                      sender window size
        Q_4_2: Is Selective Repeat used?
        SACK: Is SACK used?
        Q_4_4: Is Congestion Control used?
    """

    def parse_args(self, sender, receiver, n_bits, payloads, win,
                   Q_4_2, Q_4_3, Q_4_4, **kwargs):
        """Initialize Automaton."""
        Automaton.parse_args(self, **kwargs)
        self.win = win
        self.n_bits = n_bits
        assert self.win < 2**self.n_bits
        self.receiver = receiver
        self.sender = sender
        self.q = que.Queue()
        for item in payloads:
            self.q.put(item)

        self.buffer = {}
        self.current = 0
        self.unack = 0
        self.receiver_win = win # intialize the receiver window as sender window
        self.win_safe_to_use_min = 0 # set to 1 if it's already safe to use min(self.win, self.receiver_win) when popping from buffer
        self.Q_4_2 = Q_4_2
        self.prev_ack = -1 # the number of previous ack, initialize to -1 as an impossible sequence number.
        self.duplicated_times = 0 # number of times the same ack is received 
        self.SACK = Q_4_3
        self.Q_4_4 = Q_4_4
        self.cwnd = float(1) # congestion window
        self.ssthresh = float('inf') # congestion slow start threshold

    def master_filter(self, pkt):
        """Filter packets of interest.

        Source has be the receiver and both IP and GBN headers are required.
        No ICMP packets.
        """
        return (IP in pkt and pkt[IP].src == self.receiver and GBN in pkt
                and ICMP not in pkt)

    @ATMT.state(initial=1)
    def BEGIN(self):
        """Start state of the automaton."""
        raise self.SEND()

    @ATMT.state(final=1)
    def END(self):
        """End state of the automaton."""
        log.debug("All packets successfully transmitted!")

    @ATMT.state()
    def SEND(self):
        """Main state of sender.

        New packets are transmitted to the receiver as long as there is space
        in the window.
        """
        # check if you still can send new packets to the receiver
        if len(self.buffer) < min(self.win, self.receiver_win):
            try:
                # get next payload (automatically removes it from queue)
                payload = self.q.get(block=False)
                log.debug("Sending packet num: %s", self.current)

                # add the current segment to the buffer
                self.buffer[self.current] = payload
                log.debug("Current buffer size: %s", len(self.buffer))

                ###############################################################
                # TODO:                                                       #
                # create a GBN header with the correct header field values    #
                # send a packet to the receiver containing the created header #
                # and the corresponding payload                               #
                ###############################################################

                # send with the proper window size
                proper_win = min(self.win,self.receiver_win)

                # Q4.4 the window size should be less than cwnd
                if self.Q_4_4 == 1:
                    proper_win =min(proper_win, int(self.cwnd))
                
                if self.Q_4_4 == 1:
                    with open("CWND.txt", "a") as text_file:
                        text_file.write("%f\t%f\t%f\n" % (self.cwnd,self.ssthresh, time.time()))

                # set the header for data segment, options=1 of SACK is used.
                header_GBN = GBN(type="data",
                                 options=self.SACK,
                                 len=len(payload),
                                 hlen=6,
                                 num=self.current,
                                 win=proper_win)
                send(IP(src=self.sender,dst=self.receiver) / header_GBN / payload)

                # sequence number of next packet
                self.current = int((self.current + 1) % 2**self.n_bits)

                # back to the beginning of the state
                # (send next packet if possible)
                raise self.SEND()

            # no more payload pieces in the queue --> if all are acknowledged,
            # we can end the sender
            except que.Empty:
                if self.unack == self.current:
                    raise self.END()

    @ATMT.receive_condition(SEND)
    def packet_in(self, pkt):
        """Transition: Packet coming in from the receiver"""
        log.debug("Received packet: %s", pkt.getlayer(GBN).num)
        raise self.ACK_IN(pkt)

    @ATMT.state()
    def ACK_IN(self, pkt):
        """State for received ACK."""
        # check if type is ACK
        if pkt.getlayer(GBN).type == 0:
            log.error("Error: data type received instead of ACK %s", pkt)
            raise self.SEND()
        else:
            log.debug("Received ACK %s", pkt.getlayer(GBN).num)

            # set the receiver window size to the received value
            self.receiver_win = pkt.getlayer(GBN).win

            ack = pkt.getlayer(GBN).num

            ################################################################
            # TODO:                                                        #
            # remove all the acknowledged sequence numbers from the buffer #
            # make sure that you can handle a sequence number overflow     #
            ################################################################

            ####################################

            # The following if clause is used to identify if sender receives the receiver window size.
            # Think about this case: if sender window is much larger than receiver window, and RTT is large.
            # Then the sender will send all packets in window size min(self.win, self.receiver_win), which is equal
            # to sender window size at the beginning (calibrated after ack received). 
            # Problem is raised when sender receiving acks. It deletes acked packets from the buffer.
            # We use "for index in range(self.unack-min(self.win, self.receiver_win), self.unack):" to iterate in the buffer. However, the
            # current min(self.win, self.receiver_win) is smaller, which might cause some packets left in buffer.
            # The test scripts does not consider this. But it's an actual problem
            # Our solution: 
            # use variable win_safe_to_use_min and possible_win to avoid this problem.

            # if using min window size is already safe (win_safe_to_use_min=1), ignore the if clause.
            if self.win_safe_to_use_min == 0:
                if self.receiver_win > self.win:
                    # if receiver window is larger, then no problem in the first window size packets.
                    self.win_safe_to_use_min = 1
                elif ack > self.win:
                    # the first time ack > senderwindow, certainly min(self.win, self.receiver_win) is safe
                    # for more detailed explanation, see report.
                    self.win_safe_to_use_min = 1

            if self.win_safe_to_use_min == 1:
                possible_win = min(self.win, self.receiver_win)
            else:
                # use sender window, actually same as max(self.win, self.receiver_win)
                possible_win = self.win
            
            ####################################


            # deal with delayed ack
            # if we receive the ack in the desired range [unack, unack + possible_win + 1), duplicated acks are considered.
            if self.unack + possible_win + 1 > 2**self.n_bits:
                #overflow
                effective_ack = ack >= self.unack or ack < self.unack + possible_win + 1
            else: 
                # no overflow
                effective_ack = ack >= self.unack and ack < self.unack + possible_win + 1

            # Determine if the ack is out of date.
            if effective_ack == 0:
                # back to SEND state
                raise self.SEND()
            
            # we received an effective ack

            # update unack
            self.unack = ack
            
            # remove acked packets from buffer (may be multiple ones)
            # only consider those inside desired window
            for index in range(self.unack-possible_win, self.unack):
                # deal with overflow
                index_mod = index % 2**self.n_bits
                if index_mod in self.buffer:
                    self.buffer.pop(index_mod)


            # Q4.2 and Q4.4 share the same duplicate ack counter
            # operating correctly in all possible settings 
            # (e.g. Q_4_2 == 1 and self.Q_4_4 == 1, also Q_4_2 == 0 and self.Q_4_4 == 1, etc.)
            if self.Q_4_2 == 1 or self.Q_4_4 == 1:
                # determine if the ack is in the desired range [self.current-possible_win, self.current) 
                # If yes, ack_in_win = 1, else 0
                if self.current < possible_win:
                    ack_in_win = ack >= (self.current-possible_win) % 2**self.n_bits or ack < self.current
                else:
                    ack_in_win = ack >= self.current-possible_win and ack < self.current
                
                if ack_in_win == 1:
                    if ack == self.prev_ack:
                        # duplicated ack
                        self.duplicated_times += 1
                        log.debug("Receive ack %s for the %s time", ack, self.duplicated_times)
                        
                        # branching of Q4.4 should be in front of Q4.2 because we reset self.duplicated_times in Q4.2!
                        if self.Q_4_4 == 1 and self.duplicated_times >= 3:
                            self.ssthresh = self.cwnd / 2.0
                            self.cwnd = self.ssthresh
                            # if cwnd < 1, it's not reasonable, set to 1
                            if self.cwnd < 1:
                                self.cwnd = float(1)
                            log.debug("Congestion control: CWND fast recovery to  %s", self.cwnd)
                            log.debug("Congestion control: slow start threshold set to %s", self.ssthresh)
                            with open("CWND.txt", "a") as text_file:
                                text_file.write("%f\t%f\t%f\n" % (self.cwnd,self.ssthresh, time.time()))
                            if self.Q_4_2 == 0:
                                # if Q4.2 is not on, while Q4.4 is, we reset self.duplicated_times here.
                                # This is not suggested in lecture notes, but I think it's reasonable to reset the counter.
                                self.prev_ack = -1
                                self.duplicated_times = 1 


                        if self.Q_4_2 == 1:
                            # resend if duplicated = 3
                            if self.duplicated_times == 3:
                                pl = self.buffer[ack]
                                header_GBN = GBN(type="data",
                                    options=0,
                                    len=len(pl),
                                    hlen=6,
                                    num=ack,
                                    win=min(self.win,self.receiver_win))
                                send(IP(src=self.sender,dst=self.receiver) / header_GBN / pl)
                                log.debug("Fast resend packet: %s", ack)
                                # reset record
                                self.prev_ack = -1
                                self.duplicated_times = 1 
                        
                    else:
                        # not duplicated

                        # reset record
                        self.prev_ack = ack
                        self.duplicated_times = 1

                        # Q4.4 
                        if self.Q_4_4 == 1:
                            if self.cwnd < self.ssthresh:
                                self.cwnd += 1.0
                            else:
                                self.cwnd += 1.0 / self.cwnd
                            with open("CWND.txt", "a") as text_file:
                                text_file.write("%f\t%f\t%f\n" % (self.cwnd,self.ssthresh, time.time()))


            # Q 4.3.2 only need to check hlen, no need to determine if receiver support sack
            if self.SACK == 1:
                header_len = pkt.getlayer(GBN).hlen
                send_list = []
                if header_len > 6:
                    # resend packets in range [unack ,  left edge of 1st block)
                    first_elem = self.unack
                    if pkt.getlayer(GBN).left_1 < first_elem:
                        # overflow
                        send_list = list(range(first_elem, 2**self.n_bits))
                        send_list.extend(range(0, pkt.getlayer(GBN).left_1))
                    else:
                        # no overflow
                        send_list = list(range(first_elem, pkt.getlayer(GBN).left_1))

                # between first and second block
                # [left edge of 1st block + length of 1st block  ,  left edge of 2nd block)
                if header_len > 9:
                    first_elem = (pkt.getlayer(GBN).left_1 + pkt.getlayer(GBN).len_1) % 2**self.n_bits
                    if pkt.getlayer(GBN).left_2 < first_elem:
                        # overflow
                        send_list.extend(range(first_elem, 2**self.n_bits))
                        send_list.extend(range(0, pkt.getlayer(GBN).left_2))
                    else:
                        # no overflow
                        send_list.extend(range(first_elem, pkt.getlayer(GBN).left_2))

                # between second and third block 
                # [left edge of 2nd block + length of 2nd block  ,  left edge of 3rd block)
                if header_len > 12:
                    first_elem = (pkt.getlayer(GBN).left_2 + pkt.getlayer(GBN).len_2) % 2**self.n_bits
                    if pkt.getlayer(GBN).left_3 < first_elem:
                        # overflow
                        send_list.extend(range(first_elem, 2**self.n_bits))
                        send_list.extend(range(0, pkt.getlayer(GBN).left_3))
                    else:
                        # no overflow
                        send_list.extend(range(first_elem, pkt.getlayer(GBN).left_3))

                for idx in send_list:
                    payload = self.buffer[idx]
                    header_GBN = GBN(type="data",
                                    options=self.SACK,
                                    len=len(payload),
                                    hlen=6,
                                    num=idx,
                                    win=min(self.win,self.receiver_win))
                    send(IP(src=self.sender,dst=self.receiver) / header_GBN / payload)
                    log.debug("SACK resend packet: %s", idx)



        # back to SEND state
        raise self.SEND()

    @ATMT.timeout(SEND, TIMEOUT)
    def timeout_reached(self):
        """Transition: Timeout is reached for first unacknowledged packet."""
        log.debug("Timeout for sequence number %s", self.unack)
        if self.Q_4_4 == 1:
            self.ssthresh = self.cwnd / 2.0
            self.cwnd = float(1)
            log.debug("Congestion control: CWND multiplicative decrease to  %s", self.cwnd)
            log.debug("Congestion control: slow start threshold set to %s", self.ssthresh)
            with open("CWND.txt", "a") as text_file:
                text_file.write("%f\t%f\t%f\n" % (self.cwnd,self.ssthresh, time.time()))

        raise self.RETRANSMIT()

    @ATMT.state()
    def RETRANSMIT(self):
        """State for retransmitting packets."""

        ##############################################
        # TODO:                                      #
        # retransmit all the unacknowledged packets  #
        # (all the packets currently in self.buffer) #
        ##############################################

        for index,payload in self.buffer.items():
            header_GBN = GBN(type="data",
                                 options=self.SACK,
                                 len=len(payload),
                                 hlen=6,
                                 num=index,
                                 win=min(self.win,self.receiver_win))
            send(IP(src=self.sender,dst=self.receiver) / header_GBN / payload)



        # back to SEND state
        raise self.SEND()


if __name__ == "__main__":
    # get input arguments
    parser = argparse.ArgumentParser('GBN sender')
    parser.add_argument('sender_IP', type=str,
                        help='The IP address of the sender')
    parser.add_argument('receiver_IP', type=str,
                        help='The IP address of the receiver')
    parser.add_argument('n_bits', type=int,
                        help='The number of bits used to encode the sequence '
                             'number field')
    parser.add_argument('input_file', type=str,
                        help='Path to the input file')
    parser.add_argument('window_size', type=int,
                        help='The window size of the sender')
    parser.add_argument('Q_4_2', type=int,
                        help='Use Selective Repeat (question 4.2)')
    parser.add_argument('Q_4_3', type=int,
                        help='Use Selective Acknowledgments (question 4.3)')
    parser.add_argument('Q_4_4', type=int,
                        help='Use Congestion Control (question 4.4/Bonus)')
    parser.add_argument('--interface', type=str, help='(optional) '
                        'interface to listen on')

    args = parser.parse_args()

    if args.interface:
        conf.iface = args.interface

    bits = args.n_bits
    assert bits <= 8

    in_file = args.input_file
    # list for binary payload
    payload_to_send_bin = list()
    # chunk size of payload
    chunk_size = 2**6

    # fill payload list
    with open(in_file, "rb") as file_in:
        while True:
            chunk = file_in.read(chunk_size)
            if not chunk:
                break
            payload_to_send_bin.append(chunk)

    # initial setup of automaton
    GBN_sender = GBNSender(args.sender_IP, args.receiver_IP, bits,
                           payload_to_send_bin, args.window_size, args.Q_4_2,
                           args.Q_4_3, args.Q_4_4)

    # start automaton
    GBN_sender.run()
