#!/usr/bin/env python3

"""
    powerfence.py
    PowerFence: Optics Power Budget Management for SONiC
    
    This module implements zone-based power budget management for optical transceivers.
    It tracks power allocation per zone and controls module admission based on available headroom.
"""

import json
import os
import re
from sonic_py_common import device_info, logger

# Global logger instance
SYSLOG_IDENTIFIER = "PowerFence"
helper_logger = logger.Logger(SYSLOG_IDENTIFIER)


class PowerZone:
    """Represents a power zone with budget tracking and port management."""
    
    def __init__(self, name, ports, budget_watts, description=""):
        self.name = name
        self.port_set = set(ports)          # all ports in zone
        self.budget_watts = budget_watts
        self.description = description
        self.admitted = {}                   # port -> power_watts
        self.fenced = {}                     # port -> {power_watts, reason, media_type, speed, ...}
        self.port_priority = {}              # port -> int (lower = higher priority)

    @property
    def allocated_watts(self):
        """Returns total allocated power in watts."""
        return sum(self.admitted.values())

    @property
    def headroom(self):
        """Returns available headroom in watts."""
        return self.budget_watts - self.allocated_watts
    
    @property
    def admitted_count(self):
        """Returns count of admitted ports."""
        return len(self.admitted)
    
    @property
    def fenced_count(self):
        """Returns count of fenced ports."""
        return len(self.fenced)


class PowerFenceManager:
    """
    Manages power zones and controls module admission based on power budgets.
    
    This class is instantiated by xcvrd and hooked into CmisManagerTask to
    control module power-up based on zone power budgets.
    """
    
    # STATE_DB table names
    ZONE_TABLE = "POWERFENCE_ZONE_TABLE"
    PORT_TABLE = "POWERFENCE_PORT_TABLE"
    
    def __init__(self):
        self.zones = {}                      # zone_name -> PowerZone
        self.port_to_zone = {}               # port -> zone_name
        self.state_db = None
        self.config_loaded = False
        self.config_source = None
        
    def load_config(self):
        """
        Load power zone configuration.
        Tries power_zones.json first (community), then platform-def.json (e-sonic).
        Returns True if config was loaded successfully, False otherwise.
        """
        try:
            platform_path, hwsku_path = device_info.get_paths_to_platform_and_hwsku_dirs()
            
            # Community path: standalone power_zones.json file
            for path in [os.path.join(hwsku_path, 'power_zones.json') if hwsku_path else None,
                         os.path.join(platform_path, 'power_zones.json') if platform_path else None]:
                if path and os.path.isfile(path):
                    with open(path) as f:
                        config = json.load(f)
                    self.config_source = path
                    return self._load_from_dict(config)
            
            # e-sonic path: embedded in platform-def.json
            for path in [os.path.join(hwsku_path, 'platform-def.json') if hwsku_path else None,
                         os.path.join(platform_path, 'platform-def.json') if platform_path else None]:
                if path and os.path.isfile(path):
                    with open(path) as f:
                        data = json.load(f)
                    if 'power-zones' in data:
                        self.config_source = path
                        return self._load_from_dict(data)
            
            helper_logger.log_notice("PowerFence: no power zone config found, inactive")
            return False
            
        except Exception as e:
            helper_logger.log_error("PowerFence: failed to load config: {}".format(str(e)))
            return False
    
    def _load_from_dict(self, config):
        """Load configuration from a dictionary."""
        try:
            # Parse power zones
            pz = config.get('power-zones', {})
            for zone_name, zone_cfg in pz.items():
                ports = self._expand_port_range(zone_cfg['ports'])
                zone = PowerZone(
                    zone_name, 
                    ports, 
                    float(zone_cfg['budget_watts']),
                    zone_cfg.get('description', '')
                )
                self.zones[zone_name] = zone
                for port in ports:
                    self.port_to_zone[port] = zone_name
            
            # Load default priorities
            defaults = config.get('power-zone-port-defaults', {})
            for port_range, cfg in defaults.items():
                for port in self._expand_port_range(port_range):
                    zone_name = self.port_to_zone.get(port)
                    if zone_name:
                        self.zones[zone_name].port_priority[port] = cfg.get('priority', 128)
            
            # Initialize STATE_DB connection
            self._init_state_db()
            
            self.config_loaded = True
            helper_logger.log_notice("PowerFence: loaded {} zones from {}".format(
                len(self.zones), self.config_source))
            
            # Publish initial state
            for zone in self.zones.values():
                self._publish_zone_state(zone)
            
            # Scan existing transceivers and admit them
            self._scan_existing_transceivers()
            
            return True
            
        except Exception as e:
            helper_logger.log_error("PowerFence: failed to parse config: {}".format(str(e)))
            return False
    
    def _init_state_db(self):
        """Initialize STATE_DB connection."""
        try:
            from swsscommon import swsscommon
            self.state_db = swsscommon.SonicV2Connector()
            self.state_db.connect(self.state_db.STATE_DB)
        except Exception as e:
            helper_logger.log_error("PowerFence: failed to connect to STATE_DB: {}".format(str(e)))
            self.state_db = None
    
    def _scan_existing_transceivers(self):
        """
        Scan STATE_DB for existing transceivers and admit them to PowerFence.
        This handles the case where transceivers were inserted before PowerFence was enabled.
        """
        if self.state_db is None:
            return
        
        try:
            # Get all TRANSCEIVER_INFO keys
            xcvr_keys = self.state_db.keys(self.state_db.STATE_DB, "TRANSCEIVER_INFO|*")
            if not xcvr_keys:
                return
            
            admitted_count = 0
            for key in xcvr_keys:
                lport = key.split("|")[1]
                
                # Check if this port is in a power zone
                if lport not in self.port_to_zone:
                    continue
                
                # Get power rating from transceiver info
                power_str = self.state_db.get(self.state_db.STATE_DB, key, "power_rating_max")
                if not power_str:
                    # Try max_port_power as fallback
                    power_str = self.state_db.get(self.state_db.STATE_DB, key, "max_port_power")
                
                if not power_str:
                    continue
                
                try:
                    power_w = float(power_str)
                except (ValueError, TypeError):
                    continue
                
                if power_w <= 0:
                    continue
                
                # Get media type for logging
                media_type = self.state_db.get(self.state_db.STATE_DB, key, "form_factor") or ""
                speed = self.state_db.get(self.state_db.STATE_DB, key, "xcvr_speed_max") or ""
                
                # Try to admit the transceiver
                if self.try_admit(lport, power_w, media_type=media_type, speed=speed):
                    admitted_count += 1
            
            if admitted_count > 0:
                helper_logger.log_notice("PowerFence: admitted {} existing transceivers on startup".format(admitted_count))
                
        except Exception as e:
            helper_logger.log_error("PowerFence: failed to scan existing transceivers: {}".format(str(e)))
    
    def _expand_port_range(self, port_range_str):
        """
        Expand port range string to list of port names.
        Supports multiple formats:
          - "Ethernet0-248" -> ["Ethernet0", "Ethernet8", ..., "Ethernet248"]
          - "Ethernet0-Ethernet120" -> ["Ethernet0", "Ethernet8", ..., "Ethernet120"]
          - "Ethernet0,Ethernet8" -> ["Ethernet0", "Ethernet8"]
          - "Ethernet0-Ethernet120,Ethernet256-Ethernet360" -> combined list
        """
        ports = []
        # Handle comma-separated ranges
        for part in port_range_str.split(','):
            part = part.strip()
            
            # Check for range notation with full port names (e.g., "Ethernet0-Ethernet120")
            match_full = re.match(r'(\w+?)(\d+)-(\w+?)(\d+)$', part)
            if match_full:
                prefix1 = match_full.group(1)
                start = int(match_full.group(2))
                prefix2 = match_full.group(3)
                end = int(match_full.group(4))
                # Use prefix1 (both prefixes should be the same)
                prefix = prefix1
                # Assume 8-lane stride for OSFP ports
                stride = 8
                for i in range(start, end + 1, stride):
                    ports.append("{}{}".format(prefix, i))
                continue
            
            # Check for range notation with just end number (e.g., "Ethernet0-248")
            match_short = re.match(r'(\w+?)(\d+)-(\d+)$', part)
            if match_short:
                prefix = match_short.group(1)
                start = int(match_short.group(2))
                end = int(match_short.group(3))
                # Assume 8-lane stride for OSFP ports
                stride = 8
                for i in range(start, end + 1, stride):
                    ports.append("{}{}".format(prefix, i))
                continue
            
            # Single port (e.g., "Ethernet0")
            if part:
                ports.append(part)
        
        return ports
    
    def try_admit(self, lport, power_watts, media_type="", speed="", power_class=""):
        """
        Attempt to admit a module into its zone.
        
        Args:
            lport: Logical port name (e.g., "Ethernet0")
            power_watts: Module power requirement in watts
            media_type: Optional media type string
            speed: Optional speed string
            power_class: Optional power class string
            
        Returns:
            True if admitted, False if fenced (insufficient headroom)
        """
        if not self.config_loaded:
            return True  # PowerFence not active, allow all
            
        zone_name = self.port_to_zone.get(lport)
        if zone_name is None:
            return True  # port not in any zone, legacy behavior
        
        zone = self.zones[zone_name]
        
        # Check if already admitted with same power
        if lport in zone.admitted and zone.admitted[lport] == power_watts:
            return True
        
        # If re-admitting with different power, release old allocation first
        old_power = zone.admitted.pop(lport, 0)
        
        if zone.headroom >= power_watts:
            zone.admitted[lport] = power_watts
            zone.fenced.pop(lport, None)
            helper_logger.log_notice("PowerFence: {} admitted in zone {} ({:.2f} W, "
                              "headroom {:.2f} -> {:.2f} W)".format(
                              lport, zone_name, power_watts,
                              zone.headroom + power_watts, zone.headroom))
            self._publish_zone_state(zone)
            self._publish_port_state(lport, zone, "admitted", power_watts, media_type, speed)
            return True
        else:
            # Restore old allocation if any
            if old_power > 0:
                zone.admitted[lport] = old_power
            
            reason = "needs {:.2f} W, headroom {:.2f} W".format(power_watts, zone.headroom)
            zone.fenced[lport] = {
                'power_watts': power_watts,
                'reason': reason,
                'media_type': media_type,
                'speed': speed,
                'power_class': power_class,
            }
            zone.admitted.pop(lport, None)
            helper_logger.log_warning("PowerFence: {} FENCED in zone {} ({})".format(
                              lport, zone_name, reason))
            self._publish_zone_state(zone)
            self._publish_port_state(lport, zone, "fenced", power_watts, media_type, speed, reason)
            return False
    
    def on_remove(self, lport):
        """
        Handle module removal - release power allocation.
        
        Args:
            lport: Logical port name
        """
        if not self.config_loaded:
            return
            
        zone_name = self.port_to_zone.get(lport)
        if zone_name is None:
            return
        
        zone = self.zones[zone_name]
        released = zone.admitted.pop(lport, 0)
        zone.fenced.pop(lport, None)
        
        if released > 0:
            helper_logger.log_notice("PowerFence: {} removed from zone {} "
                              "(released {:.2f} W, headroom now {:.2f} W)".format(
                              lport, zone_name, released, zone.headroom))
            self._publish_zone_state(zone)
            self._delete_port_state(lport)
            # Try to rebalance - admit fenced ports if headroom available
            self._rebalance(zone)
    
    def _rebalance(self, zone):
        """
        Try to admit fenced ports when headroom becomes available.
        Ports are admitted in priority order (lower number = higher priority).
        """
        if not zone.fenced:
            return
        
        # Sort fenced ports by priority
        fenced_list = sorted(zone.fenced.items(), 
                            key=lambda x: zone.port_priority.get(x[0], 128))
        
        for lport, info in fenced_list:
            power_watts = info['power_watts']
            if zone.headroom >= power_watts:
                # Can admit this port
                zone.admitted[lport] = power_watts
                del zone.fenced[lport]
                helper_logger.log_notice("PowerFence: {} auto-admitted after rebalance "
                                  "({:.2f} W, headroom {:.2f} W)".format(
                                  lport, power_watts, zone.headroom))
                self._publish_zone_state(zone)
                self._publish_port_state(lport, zone, "admitted", power_watts,
                                        info.get('media_type', ''), info.get('speed', ''))
    
    def _publish_zone_state(self, zone):
        """Publish zone state to STATE_DB."""
        if self.state_db is None:
            return
        try:
            key = "{}|{}".format(self.ZONE_TABLE, zone.name)
            self.state_db.set(self.state_db.STATE_DB, key, "budget_watts", str(zone.budget_watts))
            self.state_db.set(self.state_db.STATE_DB, key, "allocated_watts", str(zone.allocated_watts))
            self.state_db.set(self.state_db.STATE_DB, key, "headroom_watts", str(zone.headroom))
            self.state_db.set(self.state_db.STATE_DB, key, "admitted_count", str(zone.admitted_count))
            self.state_db.set(self.state_db.STATE_DB, key, "fenced_count", str(zone.fenced_count))
            self.state_db.set(self.state_db.STATE_DB, key, "description", zone.description)
            # Set status based on fenced count
            if zone.fenced_count > 0:
                status = "WARNING"
            else:
                status = "OK"
            self.state_db.set(self.state_db.STATE_DB, key, "status", status)
        except Exception as e:
            helper_logger.log_error("PowerFence: failed to publish zone state: {}".format(str(e)))
    
    def _publish_port_state(self, lport, zone, status, power_watts, media_type="", speed="", reason=""):
        """Publish port state to STATE_DB."""
        if self.state_db is None:
            return
        try:
            key = "{}|{}".format(self.PORT_TABLE, lport)
            self.state_db.set(self.state_db.STATE_DB, key, "zone", zone.name)
            self.state_db.set(self.state_db.STATE_DB, key, "status", status)
            self.state_db.set(self.state_db.STATE_DB, key, "power_watts", str(power_watts))
            self.state_db.set(self.state_db.STATE_DB, key, "media_type", media_type)
            self.state_db.set(self.state_db.STATE_DB, key, "speed", speed)
            if reason:
                self.state_db.set(self.state_db.STATE_DB, key, "reason", reason)
        except Exception as e:
            helper_logger.log_error("PowerFence: failed to publish port state: {}".format(str(e)))
    
    def _delete_port_state(self, lport):
        """Delete port state from STATE_DB."""
        if self.state_db is None:
            return
        try:
            key = "{}|{}".format(self.PORT_TABLE, lport)
            self.state_db.delete(self.state_db.STATE_DB, key)
        except Exception as e:
            helper_logger.log_error("PowerFence: failed to delete port state: {}".format(str(e)))
    
    def get_zone_summary(self):
        """Return summary of all zones for CLI display."""
        summary = []
        for zone_name, zone in self.zones.items():
            summary.append({
                'name': zone_name,
                'budget_watts': zone.budget_watts,
                'allocated_watts': zone.allocated_watts,
                'headroom_watts': zone.headroom,
                'admitted_count': zone.admitted_count,
                'fenced_count': zone.fenced_count,
                'description': zone.description
            })
        return summary
    
    def get_zone_detail(self, zone_name):
        """Return detailed info for a specific zone."""
        zone = self.zones.get(zone_name)
        if zone is None:
            return None
        return {
            'name': zone_name,
            'budget_watts': zone.budget_watts,
            'allocated_watts': zone.allocated_watts,
            'headroom_watts': zone.headroom,
            'admitted': dict(zone.admitted),
            'fenced': dict(zone.fenced),
            'description': zone.description
        }
    
    def get_fenced_ports(self):
        """Return list of all fenced ports across all zones."""
        fenced = []
        for zone_name, zone in self.zones.items():
            for lport, info in zone.fenced.items():
                fenced.append({
                    'port': lport,
                    'zone': zone_name,
                    'power_watts': info['power_watts'],
                    'reason': info.get('reason', ''),
                    'media_type': info.get('media_type', ''),
                    'speed': info.get('speed', '')
                })
        return fenced
    
    def get_all_ports(self):
        """Return list of all tracked ports (admitted + fenced)."""
        ports = []
        for zone_name, zone in self.zones.items():
            for lport, power in zone.admitted.items():
                ports.append({
                    'port': lport,
                    'zone': zone_name,
                    'status': 'admitted',
                    'power_watts': power
                })
            for lport, info in zone.fenced.items():
                ports.append({
                    'port': lport,
                    'zone': zone_name,
                    'status': 'fenced',
                    'power_watts': info['power_watts'],
                    'reason': info.get('reason', '')
                })
        return sorted(ports, key=lambda x: x['port'])


# Global singleton instance
_powerfence_mgr = None

def get_powerfence_manager():
    """Get or create the global PowerFenceManager instance."""
    global _powerfence_mgr
    if _powerfence_mgr is None:
        _powerfence_mgr = PowerFenceManager()
    return _powerfence_mgr
