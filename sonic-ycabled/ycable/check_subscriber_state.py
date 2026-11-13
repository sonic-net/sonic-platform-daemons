import swsssdk
from swsscommon import swsscommon

PORTS=['Ethernet0' ,'Ethernet4' ,'Ethernet8', 'Ethernet12' ,'Ethernet16','Ethernet20', 'Ethernet40', 'Ethernet44', 'Ethernet48', 'Ethernet52', 'Ethernet56', 'Ethernet60', 'Ethernet64', 'Ethernet68', 'Ethernet72', 'Ethernet76', 'Ethernet80',  'Ethernet84', 'Ethernet104', 'Ethernet108', 'Ethernet112' ,'Ethernet116', 'Ethernet120' , 'Ethernet124']


def main():
    print("running the script subscriber")
    db = swsscommon.SonicV2Connector(use_unix_socket_path=True, namespace='')
    state_db = swsscommon.DBConnector('STATE_DB', 0, True, '')
    print("got the DB subscriber")
    #print(dir(db))
    db.connect('APPL_DB')
    print("connected to DB")
    redisclient = db.get_redis_client("APPL_DB")
    print("got the redis client")
    pubsub = redisclient.pubsub()
    dbid = db.get_dbid("APPL_DB")
    pubsub.psubscribe("__keyspace@{}__:MUX_CABLE_COMMAND_TABLE*".format(dbid))
    probe_record_tbl = {}
    probe_record_tbl = swsscommon.Table(state_db, "XCVRD_MUX_SCRIPT_STATS")
    count = 0
    for port in PORTS:
        fvs_log = swsscommon.FieldValuePairs([(str("count"), str(count))])
        probe_record_tbl.set(port, fvs_log)
    full_count = 100000
    while True:
        full_count = full_count - 1
        if full_count < 0:
            break
        item = pubsub.listen_message()
        if 'type' in item and item['type'] == 'pmessage':
            key = item['channel'].split(':', 1)[1]
            port = key.split(':')[1]
            (status, fvs) = probe_record_tbl.get(port)
            if status is False:
                print("Could not retreive fieldvalue pairs for {}, inside state_db table".format(port))
            else:
                mux_port_dict = dict(fvs)
                prev_count = int(mux_port_dict.get("count", 0)) + 1 

                fvs_log = swsscommon.FieldValuePairs([(str("count"), str(prev_count))])
                probe_record_tbl.set(port, fvs_log)




if __name__ == '__main__':
    main()
