# Python ycabled unit tests and their ycabled-rs counterparts

Python ycabled's unit tests (`sonic-ycabled/tests`) have 320 test definitions with 284 distinct names, in `test_y_cable_helper.py`, `test_ycable.py` and `test_grpc_ipv6.py`. Some names are defined twice, and the later definition replaces the earlier one. The tests call Python's functions directly and mock its internals: module globals, threads and `swsscommon` objects.

ycabled-rs has different internals, so its unit tests check the same behaviors at its own seams:

- **Most tests** run a port actor against mocks of the platform, the cables, the SoC and the database (`src/testing.rs`). They give it the message a table event would produce and check what it writes and which driver calls or RPCs it makes.
- **Behavior at a boundary** is tested against the real thing (`src/tests/`): a private `redis-server`, an in-process gRPC server, and the embedded interpreter with fake `sonic_y_cable` and platform modules.

Each Rust test's doc comment names the Python tests it covers. The black-box suite (`sonic-ycabled/blackbox`) runs the same tests on both daemons and checks the end-to-end behavior against Python's.

Run the unit tests with `./build.sh cargo test --release --lib` (see [README.md](../README.md)).

## By area

| Area | Python tests | Rust tests |
| --- | ---: | ---: |
| [Driver selection and presence](#driver-selection-and-presence) | 46 | 15 |
| [Insertion, removal and startup](#insertion-removal-and-startup) | 20 | 10 |
| [Active-standby toggle](#active-standby-toggle) | 16 | 9 |
| [Active-standby probe](#active-standby-probe) | 11 | 4 |
| [Active-active: channel, initialization, GracefulRestart](#active-active-channel-initialization-gracefulrestart) | 28 | 22 |
| [Active-active: requests and replies](#active-active-requests-and-replies) | 25 | 9 |
| [Telemetry](#telemetry) | 22 | 17 |
| [CLI commands](#cli-commands) | 105 | 27 |
| [Daemon, threads, DB and logging](#daemon-threads-db-and-logging) | 11 | 15 |

The Rust counts are lower mostly because one table-driven Rust test covers a family of Python tests. For example, `config_prbs_modes` covers all seven `test_handle_config_prbs_cmd_arg_tbl_notification_with_instance_*` tests. A few Rust tests appear in more than one area.

### Driver selection and presence

Python: `test_check_identifier_presence_and_update_mux_table_entry*` (14), `test_check_identifier_presence_marks_hardware_y_cable_initialized`, `test_check_identifier_presence_does_not_mark_failed_simulator_initialized`, `test_is_initialized_y_cable*` (4), `test_y_cable_wrapper_get_presence*` (3), `test_y_cable_wrapper_get_transceiver_info*` (3), `test_y_cable_helper_format_mapping_identifier*` (2), `test_get_ycable_port_instance_from_logical_port*` (4), `test_get_ycable_physical_port_from_logical_port*` (3), `test_check_mux_cable_port_type*` (3), `test_read_y_cable_and_update_statedb_port_tbl*` (8).

Rust:

- `tests::bridge::bridge_selects_and_calls_drivers`: the vendor mapping, every reason a driver cannot be used, the `get_vendor()` check, the simulator override, presence with and without the platform API.
- `actor::tests::standby_init_publishes_cable_state`, `standby_init_read_side_b`, `standby_init_absent_cable`, `standby_init_without_driver`, `standby_init_read_side_unreadable`, `standby_init_mux_direction_unreadable`, `standby_uninitialized_driver_is_loaded_again`, `standby_init_after_another_port_writes_no_info`, `standby_startup_init_reports_presence`, `active_on_vs_is_present`.
- `config::tests::cable_types`; `ports::tests::config_db_ports`, `port_config_file`, `natural_order`.

Not ported: `_multiple_port_instances` and the other cases of a logical port that maps to several physical ports. Mux ports are never broken out, and the port map is one to one.

### Insertion, removal and startup

Python: `test_change_ports_status_for_y_cable_change_event*` (5), `test_check_identifier_presence_and_delete_mux_table_entry`, `test_create_tables_and_insert_mux_unknown_entries`, `test_delete_port_from_y_cable_table`, `test_delete_ports_status_for_y_cable`, `test_detect_port_in_error_status`, `test_handle_state_update_task*` (5), `test_ycable_helper_state_update_task`, `test_init_ports_status_for_y_cable`, `test_process_loopback_interface_and_get_read_side*` (3).

Rust:

- `actor::tests::standby_removal_keeps_read_side`, `standby_insertion_after_removal_loads_driver_again`, `removal_without_row_writes_nothing`, `active_removal_and_reinsertion`, `standby_uninitialized_driver_is_loaded_again`, `telemetry_overlapping_removal_is_discarded`.
- `tests::daemon::first_initialized_port_writes_mux_cable_info`: Python's `y_cable_presence` at startup.
- `tests::daemon::rows_become_actor_messages`, `config::tests::read_side_mapping`, `tests::redis::subscription_replays_rows_then_follows_changes`.

Not ported:

- `test_delete_ports_status_for_y_cable`: Python's `deinit()`, which deletes tables at shutdown, never runs (HLD 3.8 issue 1). ycabled-rs keeps the tables (C7), and the black-box `test_lifecycle.py` checks it.
- `test_detect_port_in_error_status`: nothing calls this function.

### Active-standby toggle

Python: `test_update_tor_active_side*` (8), `test_y_cable_toggle_mux_tor*` (6), `test_ycable_helper_table_worker`, `test_ycable_helper_table_worker_toggle`.

Rust: `actor::tests::standby_toggle_matrix`, `standby_toggle_failure_writes_unknown`, `standby_toggle_reads_side_again`, `standby_toggle_read_side_still_unreadable`, `standby_toggle_cable_gone`, `standby_toggle_without_driver`, `standby_toggle_ignores_other_requests`, `mailbox_keeps_order`, `toggle_overtakes_telemetry`.

Not ported: `test_update_tor_active_side_multiple_mappings` (breakout, see above).

### Active-standby probe

Python: `test_update_appdb_port_mux_cable_response_table*` (8), `test_handle_ycable_active_standby_probe_notification`, `test_ycable_helper_table_worker_probe`, `test_ycable_helper_table_worker_probe_active`.

Rust: `actor::tests::standby_probe_matrix`, `standby_probe_unknown_cases`, `standby_read_side_nic_is_unknown`, `standby_probe_ignores_other_commands`.

Not ported: `_invalid_ycable_mapping` (breakout).

### Active-active: channel, initialization, GracefulRestart

Python: `test_check_identifier_presence_and_setup_channel*` (4), `test_setup_grpc_channel_for_port*` (2), `test_setup_grpc_channels`, `test_retry_setup_grpc_channel_for_port*` (3), `test_connect_channel`, `test_get_grpc_credentials*` (2), `test_apply_grpc_secrets_configuration*` (2), `test_format_grpc_target_for_ipv4` and `_ipv6`, `test_get_soc_ip_for_grpc_*` (2), the 6 tests of `test_grpc_ipv6.py`, `test_check_presence_for_active_active_cable_type`, `test_ycable_graceful_client`, `test_ycable_wait_for_state_change`.

Rust:

- `actor::tests::active_init_from_soc`, `active_init_failures`, `active_init_absent_cable`, `active_init_without_soc_address`, `active_init_ipv6_soc`, `active_init_sets_presence`, `active_toggle_retries_channel`, `active_on_vs_is_present`.
- `tests::grpc::rpcs_reach_the_soc`, `rpcs_end_at_the_deadline`, `refused_connection_writes_failure`, `lost_connection_writes_failure_and_reconnects`, `ipv6_soc`, `secure_config_without_certs_gives_no_channel`, `graceful_restart_subscribes_again_after_period`, `graceful_restart_lower_tor`.
- `grpc::tests::targets`, `states_from_events`, `tor_sides`; `config::tests::channel_config_variants`, `secrets_fill_missing_fields_only`, `soc_addresses`.

Not ported: `test_connect_channel` and `test_setup_grpc_channels`. Nothing calls those functions; the call to `connect_channel` is commented out.

### Active-active: requests and replies

Python: `test_ycable_helper_table_worker_active_active`, `_probe_active_active`, `_toggle_active_active`, and the 22 `test_parse_grpc_response_*` tests.

Rust: `actor::tests::active_toggle_matrix`, `active_toggle_failures`, `active_toggle_without_read_side`, `active_forwarding_probe`, `active_forwarding_probe_without_channel`; `grpc::tests::forwarding_state_by_read_side`, `link_state_by_read_side`, `set_reply`, `code_names`.

### Telemetry

Python: `test_get_muxcable_info*` (9), `test_get_muxcable_static_info*` (3), `test_post_port_mux_info_to_db`, `test_post_port_mux_static_info_to_db`, `test_check_identifier_presence_and_update_mux_info_entry*` (2), `test_get_mux_cable_info_without_presence`, `test_get_mux_cable_static_info_without_presence`, `test_handle_ycable_enable_disable_tel_notification*` (2), `test_put_all_values_from_dict_to_db`, `test_put_all_values_from_list_to_db`.

Rust:

- `actor::tests::standby_telemetry_full_info`, `standby_telemetry_driver_failures`, `standby_telemetry_during_download`, `standby_telemetry_absent_cable_placeholder`, `standby_telemetry_without_driver_writes_nothing`, `standby_static_info_failures`, `telemetry_disabled_writes_nothing`, `active_telemetry`, `active_telemetry_without_channel`, `active_telemetry_without_row`, `toggle_overtakes_telemetry`, `telemetry_overlapping_removal_is_discarded`.
- `config::tests::xcvrd_log_only_y_cable`, `schema::tests::timestamp_format`, `pyval::tests::float_str_matches_python`, `pyval::tests::str_of_containers`, `tests::redis::rows_use_sonic_keys_and_separators`.

### CLI commands

Python: the 96 `test_handle_*_cmd_arg_tbl_notification*` tests, `test_task_download_firmware_worker`, `test_set_result_and_delete_port`, `test_set_show_firmware_fields`, `test_get_firmware_dict*` (4), `test_ycable_helper_cli_worker*` (2).

| Python tests | Rust tests (`tests::cli::` unless noted) |
| --- | --- |
| `handle_show_mux_state_*` (7) | `show_mux_direction_active_standby`, `show_mux_direction_failures`, `show_mux_direction_without_input_is_not_answered`, `show_mux_direction_active_active` |
| `handle_config_mux_state_*` (5) | `config_mux_state_toggles_the_cable`, `config_mux_state_failures` |
| `handle_show_mux_switchmode_*` (5) | `show_switch_mode` |
| `handle_config_mux_switchmode_*` (6) | `config_switch_mode` |
| `handle_show_firmware_show_*` (4), `set_show_firmware_fields`, `get_firmware_dict*` (4) | `show_firmware_versions`, `show_firmware_during_download`, `show_firmware_failures` |
| `handle_config_firmware_down_*` (4), `task_download_firmware_worker` | `download_firmware`, `download_firmware_failures` |
| `handle_config_firmware_acti_*` (4) | `activate_firmware` |
| `handle_config_firmware_roll_*` (4) | `rollback_firmware` |
| `handle_config_prbs_*` (10) | `config_prbs_modes`, `config_prbs_failures` |
| `handle_config_loop_*` (5) | `config_loopback` |
| `handle_show_event_*` (6) | `show_event_log` |
| `handle_get_fec_*` (36) | `show_fec_anlt_speed`, `show_ber_family`, `show_ber_failures` |
| `ycable_helper_cli_worker*` (2), `set_result_and_delete_port` | `commands_for_other_ports`; `cli::tests::every_command_table_has_a_command`, `status_is_str_of_the_return_value`, `result_fields_by_shape` |

### Daemon, threads, DB and logging

Python: `test_DaemonYcable_init_deinit` and `_full`, `test_ycable_helper_class_run*` (3), `test_ycable_helper_async_client_run_loop_with_exception`, `test_xcvrd_helper_class_run`, `test_ycable_info_helper_class_run`, `test_update_table_mux_status_for_response_tbl`, `test_gather_arg_from_db_and_check_for_type`, `test_gather_arg_from_db_and_check_for_none_type`.

Rust:

- `tests::daemon::subscribes_to_what_python_subscribes_to`; `tests::redis::waits_for_config_db_initialized`, `subscription_replays_rows_then_follows_changes`, `rows_use_sonic_keys_and_separators`.
- `db::tests::locations_from_config`, `global_config_includes_namespaces`, `ipv6_redis_host`, `memdb_merges_rows_and_records_writes`; `ports::tests::asic_conf`, `port_config_paths`.
- `logger::tests::line_has_glibc_layout`, `levels_order_by_severity`, `sends_to_the_socket`; `tests::bridge::slow_calls_are_logged`.
- `cli::tests::int_arguments_as_python_reads_them`: the CLI's arguments, as `gather_arg_from_db_and_check_for_type` and `int()` read them.

Not ported:

- The tests of Python's thread classes and their run loops: `*_class_run*`, `async_client_run_loop_with_exception` and `DaemonYcable_init_deinit*`. ycabled-rs has tasks instead of threads. The black-box `test_lifecycle.py` covers its startup, shutdown, task failure and signals.
- `test_gather_arg_from_db_and_check_for_none_type`: nothing calls that function.
- `test_update_table_mux_status_for_response_tbl`: the probe tests above cover what it writes.
