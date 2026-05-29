#!/bin/bash
#
# Deploy chassisd changes to a running SONiC SmartSwitch for testing.
#
# This copies the modified chassisd script and restarts the service,
# avoiding a full image rebuild.
#
# Usage:
#   ./deploy_chassisd.sh <switch_ip> [ssh_user]
#
# Examples:
#   ./deploy_chassisd.sh 10.0.0.1
#   ./deploy_chassisd.sh 10.0.0.1 admin
#

set -e

SWITCH_IP="${1:?Usage: $0 <switch_ip> [ssh_user]}"
SSH_USER="${2:-admin}"
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Files to copy
CHASSISD_SRC="$REPO_ROOT/sonic-chassisd/scripts/chassisd"
TESTBED_SCRIPT="$REPO_ROOT/sonic-chassisd/tests/test_dpu_auto_recovery_testbed.py"

# Target paths on the switch
CHASSISD_DST="/usr/local/lib/python3/dist-packages/chassisd"
# Alternative location depending on image version:
CHASSISD_DST_ALT="/usr/bin/chassisd"
PMON_CONTAINER="pmon"

echo "=== Deploying chassisd to $SSH_USER@$SWITCH_IP ==="

# Step 1: Find where chassisd lives on the switch
echo "[1/5] Locating chassisd on the switch..."
CHASSISD_PATH=$(ssh $SSH_OPTS "$SSH_USER@$SWITCH_IP" \
    "docker exec $PMON_CONTAINER find / -name chassisd -path '*/scripts/*' 2>/dev/null | head -1")

if [ -z "$CHASSISD_PATH" ]; then
    echo "ERROR: Could not find chassisd in $PMON_CONTAINER container"
    echo "Trying to find it on the host..."
    CHASSISD_PATH=$(ssh $SSH_OPTS "$SSH_USER@$SWITCH_IP" \
        "find /usr -name chassisd 2>/dev/null | head -1")
fi

if [ -z "$CHASSISD_PATH" ]; then
    echo "ERROR: chassisd not found on the switch. Is this a SmartSwitch?"
    exit 1
fi
echo "  Found: $CHASSISD_PATH"

# Step 2: Backup original
echo "[2/5] Backing up original chassisd..."
ssh $SSH_OPTS "$SSH_USER@$SWITCH_IP" \
    "docker exec $PMON_CONTAINER cp $CHASSISD_PATH ${CHASSISD_PATH}.bak"
echo "  Backup: ${CHASSISD_PATH}.bak"

# Step 3: Copy new chassisd to switch
echo "[3/5] Copying modified chassisd..."
scp $SSH_OPTS "$CHASSISD_SRC" "$SSH_USER@$SWITCH_IP:/tmp/chassisd"
ssh $SSH_OPTS "$SSH_USER@$SWITCH_IP" \
    "docker cp /tmp/chassisd $PMON_CONTAINER:$CHASSISD_PATH && rm /tmp/chassisd"
echo "  Done"

# Step 4: Copy testbed script
echo "[4/5] Copying testbed script..."
scp $SSH_OPTS "$TESTBED_SCRIPT" "$SSH_USER@$SWITCH_IP:/tmp/test_dpu_auto_recovery_testbed.py"
echo "  Copied to /tmp/test_dpu_auto_recovery_testbed.py"

# Step 5: Restart chassisd
echo "[5/5] Restarting chassisd in $PMON_CONTAINER..."
ssh $SSH_OPTS "$SSH_USER@$SWITCH_IP" \
    "docker exec $PMON_CONTAINER supervisorctl restart chassisd"

echo ""
echo "=== Deployment complete ==="
echo ""
echo "To verify:"
echo "  ssh $SSH_USER@$SWITCH_IP"
echo "  docker exec $PMON_CONTAINER supervisorctl status chassisd"
echo "  sudo python3 /tmp/test_dpu_auto_recovery_testbed.py --skip-destructive"
echo ""
echo "To rollback:"
echo "  ssh $SSH_USER@$SWITCH_IP \"docker exec $PMON_CONTAINER cp ${CHASSISD_PATH}.bak $CHASSISD_PATH\""
echo "  ssh $SSH_USER@$SWITCH_IP \"docker exec $PMON_CONTAINER supervisorctl restart chassisd\""
