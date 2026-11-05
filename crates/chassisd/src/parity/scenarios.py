"""chassisd's parity scenarios, a modular chassis and a SmartSwitch: the cards (the Python
daemon's own test mocks, sonic-chassisd/tests/mock_platform.py), this card's
slot, and a sequence of passes, CONFIG_DB changes and changes to the cards.

Each scenario answers (build, steps, kind), kind 'chassis' or
'smartswitch'.  build() makes a fresh chassis -- and
writes what CONFIG_DB holds -- after the harness has emptied the tables.
Steps are ('pass',) -- `module_db_update()`, `check_midplane_reachability()`,
`module_down_chassis_db_cleanup()`, as one turn of `ChassisdDaemon.run`'s
loop -- ('config', name, 'SET' | 'DEL') -- one CHASSIS_MODULE event, as
`ConfigManagerTask` hands it to `ModuleConfigUpdater` -- or ('call', f),
f(chassis).  A ('call', record) step copies every card's admin state into a
table of its own, so what the config events did to the hardware is compared
too.  On a SmartSwitch a pass is `module_db_update()` and
`check_midplane_reachability()` -- the recovery machine runs on timers and is
left to the unit tests -- and ('boot', name, boot_id) is one boot_id the
reboot-cause subscriber hands `dpu_boot_id_update`.
"""

import glob as real_glob
import os
import shutil
import tempfile
import types

H = None
CD = None
MP = None
ALIVE = []
# Where the reboot causes and midplane-down reasons are kept; fresh per half.
REBOOT_ROOT = [None]


def setup(harness, root):
    global H, CD, MP
    H = harness
    tests = os.path.join(root, 'sonic-chassisd', 'tests')
    MP = H.load_file('chassisd_parity_mock_platform', os.path.join(tests, 'mock_platform.py'))
    CD = H.script('chassisd', os.path.join(root, 'sonic-chassisd', 'scripts', 'chassisd'))


SUPERVISOR_SLOT = 16


def card(index, name, kind, slot, status='Online', asics=(), reachable=True):
    m = MP.MockModule(index, name, '{} desc'.format(name), kind, slot, 'SN{}'.format(index),
                      list(asics))
    m.set_oper_status(status)
    m.supervisor_slot = SUPERVISOR_SLOT
    m.set_midplane_ip()
    m.set_midplane_reachable(reachable)
    return m


def chassis(my_slot, cards, hostname=None):
    c = MP.MockChassis()
    c.module_list = list(cards)
    c.get_my_slot = lambda: my_slot
    c.get_supervisor_slot = lambda: SUPERVISOR_SLOT
    if hostname is not None:
        H.Table(H.Db('CONFIG_DB'), 'DEVICE_METADATA').set('localhost', [('hostname', hostname)])
    return c


def cards():
    from sonic_platform_base.module_base import ModuleBase
    return [card(0, 'SUPERVISOR0', ModuleBase.MODULE_TYPE_SUPERVISOR, SUPERVISOR_SLOT),
            card(1, 'LINE-CARD0', ModuleBase.MODULE_TYPE_LINE, 1,
                 asics=[('4', '0000:04:00.0'), ('5', '0000:05:00.0')]),
            card(2, 'LINE-CARD1', ModuleBase.MODULE_TYPE_LINE, 2, status='Offline', reachable=False),
            card(3, 'FABRIC-CARD0', ModuleBase.MODULE_TYPE_FABRIC, 17,
                 asics=[('0', '0000:06:00.0')])]


def record(c):
    t = H.Table(H.Db('TEST'), 'ADMIN_STATE')
    for m in c.module_list:
        t.set(m.get_name(), [('admin_state', str(m.get_admin_state()))])


def card_named(c, name):
    return [m for m in c.module_list if m.get_name() == name][0]


def supervisor():
    def down(c):
        card_named(c, 'LINE-CARD0').set_oper_status('Offline')
        card_named(c, 'LINE-CARD0').set_midplane_reachable(False)

    def up(c):
        card_named(c, 'LINE-CARD0').set_oper_status('Online')
        card_named(c, 'LINE-CARD0').set_midplane_reachable(True)
        card_named(c, 'LINE-CARD1').set_oper_status('Online')
        card_named(c, 'LINE-CARD1').set_midplane_reachable(True)
    return (lambda: chassis(SUPERVISOR_SLOT, cards())), \
        [('pass',), ('call', down), ('pass',), ('call', up), ('pass',), ('pass',)], 'chassis'


def line_card():
    def lost(c):
        card_named(c, 'SUPERVISOR0').set_midplane_reachable(False)
    return (lambda: chassis(1, cards(), hostname='lc0-host')), \
        [('pass',), ('call', lost), ('pass',), ('pass',)], 'chassis'


def line_card_without_hostname():
    return (lambda: chassis(1, cards())), [('pass',)], 'chassis'


def config_events():
    return (lambda: chassis(SUPERVISOR_SLOT, cards())), \
        [('pass',),
         ('config', 'LINE-CARD0', 'SET'), ('call', record),
         ('config', 'LINE-CARD0', 'DEL'), ('call', record),
         ('config', 'LINE-CARD9', 'SET'),
         ('config', 'DPU0', 'SET'),
         ('config', 'FABRIC-CARD0', 'SET'), ('call', record),
         ('pass',)], 'chassis'


def midplane_down_at_start():
    def build():
        c = chassis(SUPERVISOR_SLOT, cards())
        c.init_midplane_switch = lambda: False
        return c
    return build, [('pass',)], 'chassis'


def reboot_root():
    return REBOOT_ROOT[0]


def teardown():
    """Remove the reboot-cause directory.  Each SmartSwitch scenario removes
    the one before it; the test calls this for the last.  Not atexit: the
    interpreter is embedded in the test process and is never finalized."""
    if REBOOT_ROOT[0]:
        shutil.rmtree(REBOOT_ROOT[0], ignore_errors=True)
        REBOOT_ROOT[0] = None


def dpus(n=4):
    out = []
    for i in range(n):
        m = card(i, 'DPU{}'.format(i), 'DPU', i, reachable=True)
        out.append(m)
    return out


def smartswitch_chassis(modules):
    teardown()
    REBOOT_ROOT[0] = tempfile.mkdtemp(prefix='chassisd-parity-')
    c = MP.MockSmartSwitchChassis()
    c.module_list = list(modules)
    return c


def smartswitch_midplane():
    # DPU1's midplane goes down unplanned, with the platform's reason; DPU2's
    # for a planned shutdown; then both come back.
    def down(c):
        c.module_list[1].set_midplane_reachable(False)
        c.module_list[1].set_midplane_down_reason('PCIe link lost')
        c.module_list[2].set_midplane_reachable(False)
        c.module_list[2].set_module_state_transition('DPU2', 'shutdown')
        H.Table(H.Db('STATE_DB'), 'CHASSIS_MODULE_TABLE').set('DPU2', [('transition_type', 'shutdown')])

    def offline(c):
        c.module_list[1].set_oper_status('Offline')

    def up(c):
        for m in c.module_list:
            m.set_midplane_reachable(True)
            m.set_oper_status('Online')
            m.clear_module_state_transition(m.get_name())
    return (lambda: smartswitch_chassis(dpus())), \
        [('pass',), ('call', down), ('pass',), ('call', offline), ('pass',), ('call', up), ('pass',)], \
        'smartswitch'


def smartswitch_reboot_causes():
    # A new boot_id is a reboot to record; the same one again is not.
    return (lambda: smartswitch_chassis(dpus(2))), \
        [('pass',), ('boot', 'DPU0', 'boot-1'), ('boot', 'DPU0', 'boot-1'), ('boot', 'DPU1', ''),
         ('boot', 'DPU9', 'boot-9'), ('boot', 'DPU0', 'boot-2'), ('pass',)], 'smartswitch'


SCENARIOS = [supervisor, line_card, line_card_without_hostname, config_events,
             midplane_down_at_start, smartswitch_midplane, smartswitch_reboot_causes]


def names():
    return [s.__name__ for s in SCENARIOS]


def scenario(name):
    return dict((s.__name__, s) for s in SCENARIOS)[name]()


def run_python(name):
    """The Python half: chassisd's own updaters, as `ChassisdDaemon.run`
    drives them on a modular chassis."""
    build, steps, kind = scenario(name)
    del ALIVE[:]
    H.reset()
    c = build()
    H.use_chassis(c)
    if kind == 'smartswitch':
        # The two places Python names the directory: its constant, and the
        # glob in `update_dpu_reboot_cause_to_db`, which spells it out.
        CD.MODULE_REBOOT_CAUSE_DIR = REBOOT_ROOT[0]
        CD.glob = types.SimpleNamespace(glob=lambda p: real_glob.glob(
            p.replace('/host/reboot-cause/module/', os.path.join(REBOOT_ROOT[0], ''), 1)))
        updater = CD.SmartSwitchModuleUpdater('chassisd', c)
    else:
        updater = CD.ModuleUpdater('chassisd', c,
                                   CD.try_get(c.get_my_slot, default=CD.INVALID_SLOT),
                                   CD.try_get(c.get_supervisor_slot, default=CD.INVALID_SLOT))
    updater.modules_num_update()
    config = CD.ModuleConfigUpdater('chassisd', c)
    ALIVE.extend([updater, config])
    for step in steps:
        if step[0] == 'pass':
            updater.module_db_update()
            updater.check_midplane_reachability()
            if kind != 'smartswitch':
                updater.module_down_chassis_db_cleanup()
        elif step[0] == 'boot':
            updater.dpu_boot_id_update(step[1], step[2])
        elif step[0] == 'config':
            config.module_config_update(step[1], CD.MODULE_ADMIN_DOWN if step[2] == 'SET'
                                        else CD.MODULE_ADMIN_UP)
        else:
            step[1](c)
