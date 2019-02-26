import logging
import base64
from honeybadgermpc.protocols.crypto.boldyreva import serialize, deserialize1
import asyncio
from collections import defaultdict
import hashlib


class CommonCoinFailureException(Exception):
    """Raised for common coin failures."""
    pass


def hash(x):
    return hashlib.sha256(x).digest()


async def shared_coin(sid, pid, n, f, pk, sk, broadcast, receive):
    """A shared coin based on threshold signatures

    :param sid: a unique instance id
    :param pid: my id number
    :param N: number of parties
    :param f: fault tolerance, :math:`f+1` shares needed to get the coin
    :param PK: ``boldyreva.TBLSPublicKey``
    :param SK: ``boldyreva.TBLSPrivateKey``
    :param broadcast: broadcast channel
    :param receive: receive channel
    :return: a function ``getCoin()``, where ``getCoin(r)`` blocks
    """
    assert pk.k == f+1
    assert pk.l == n    # noqa: E741
    received = defaultdict(dict)
    output_queue = defaultdict(lambda: asyncio.Queue(1))

    async def _recv():
        while True:     # main receive loop
            logging.debug(f'entering loop', extra={'nodeid': pid, 'epoch': '?'})
            # New shares for some round r, from sender i
            (i, (_, r, sig_bytes)) = await receive()
            sig = deserialize1(sig_bytes)
            logging.debug(
                          f'received i, _, r, sig: {i, _, r, sig}',
                          extra={'nodeid': pid, 'epoch': r})
            assert i in range(n)
            assert r >= 0
            if i in received[r]:
                print("redundant coin sig received", (sid, pid, i, r))
                continue

            h = pk.hash_message(str((sid, r)))

            # TODO: Accountability: Optimistically skip verifying
            # each share, knowing evidence available later
            try:
                pk.verify_share(sig, i, h)
            except AssertionError:
                print("Signature share failed!", (sid, pid, i, r))
                continue

            received[r][i] = sig

            # After reaching the threshold, compute the output and
            # make it available locally
            logging.debug(
                f'if len(received[r]) == f + 1: {len(received[r]) == f + 1}',
                extra={'nodeid': pid, 'epoch': r},
            )
            if len(received[r]) == f + 1:

                # Verify and get the combined signature
                sigs = dict(list(received[r].items())[:f+1])
                sig = pk.combine_shares(sigs)
                assert pk.verify_signature(sig, h)

                # Compute the bit from the least bit of the hash
                bit = hash(serialize(sig))[0] % 2
                logging.debug(
                    f'put bit {bit} in output queue', extra={'nodeid': pid, 'epoch': r})
                output_queue[r].put_nowait(bit)

    recv_task = asyncio.create_task(_recv())

    async def get_coin(round):
        """Gets a coin.

        :param round: the epoch/round.
        :returns: a coin.

        """
        # I have to do mapping to 1..l
        h = pk.hash_message(str((sid, round)))
        logging.debug(
                      f"broadcast {('COIN', round, sk.sign(h))}",
                      extra={'nodeid': pid, 'epoch': round})
        broadcast(('COIN', round, serialize(sk.sign(h))))
        return await output_queue[round].get()

    return get_coin, recv_task


async def run_common_coin(config, pbk, pvk, n, f, nodeid):
    program_runner = ProcessProgramRunner(config, n, t, nodeid)
    sender, listener = program_runner.senders, program_runner.listener

    await sender.connect()

    send, recv = program_runner.get_send_and_recv("coin")

    def broadcast(o):
        for i in range(n):
            send(i, o)

    coin, crecv_task = await shared_coin('sidA', nodeid, n, f, pbk, pvk, broadcast, recv)
    for i in range(10):
        logging.info("%d COIN VALUE: %s", i, await coin(i))
    crecv_task.cancel()

    await sender.close()
    await listener.close()


if __name__ == "__main__":
    import os
    import sys
    import pickle
    from honeybadgermpc.exceptions import ConfigurationError
    from honeybadgermpc.config import load_config
    from honeybadgermpc.ipc import NodeDetails, ProcessProgramRunner
    from honeybadgermpc.protocols.crypto.boldyreva import TBLSPublicKey  # noqa:F401
    from honeybadgermpc.protocols.crypto.boldyreva import TBLSPrivateKey  # noqa:F401

    configfile = os.environ.get('HBMPC_CONFIG')
    nodeid = os.environ.get('HBMPC_NODE_ID')
    pvk_string = os.environ.get('HBMPC_PV_KEY')
    pbk_string = os.environ.get('HBMPC_PB_KEY')

    # override configfile if passed to command
    try:
        nodeid = sys.argv[1]
        configfile = sys.argv[2]
        pbk_string = sys.argv[3]
        pvk_string = sys.argv[4]
    except IndexError:
        pass

    if not nodeid:
        raise ConfigurationError('Environment variable `HBMPC_NODE_ID` must be set'
                                 ' or a node id must be given as first argument.')

    if not configfile:
        raise ConfigurationError('Environment variable `HBMPC_CONFIG` must be set'
                                 ' or a config file must be given as first argument.')

    if not pvk_string:
        raise ConfigurationError('Environment variable `HBMPC_PV_KEY` must be set'
                                 ' or a config file must be given as first argument.')

    if not pbk_string:
        raise ConfigurationError('Environment variable `HBMPC_PB_KEY` must be set'
                                 ' or a config file must be given as first argument.')

    config_dict = load_config(configfile)
    n = config_dict['N']
    t = config_dict['t']
    k = config_dict['k']
    pbk = pickle.loads(base64.b64decode(pbk_string))
    pvk = pickle.loads(base64.b64decode(pvk_string))
    nodeid = int(nodeid)
    network_info = {
        int(peerid): NodeDetails(addrinfo.split(':')[0], int(addrinfo.split(':')[1]))
        for peerid, addrinfo in config_dict['peers'].items()
    }

    asyncio.set_event_loop(asyncio.new_event_loop())
    loop = asyncio.get_event_loop()
    loop.set_debug(True)
    try:
        loop.run_until_complete(run_common_coin(network_info, pbk, pvk, n, t, nodeid))
    finally:
        loop.close()