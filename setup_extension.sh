#!/bin/bash
# ============================================================
# Install the CUA Cursor Tracker GNOME Shell extension.
#
# This extension exposes cursor position over D-Bus on Wayland GNOME,
# where pynput and Shell.Eval cannot access the global pointer.
#
# Usage: bash setup_extension.sh
#
# After running, you MUST log out and log back in.
# ============================================================

set -e

EXT_UUID="cursor-tracker@cua"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$EXT_UUID"

echo ""
echo "=== CUA Cursor Tracker Extension ==="
echo ""

# 1. Create extension files
mkdir -p "$EXT_DIR"

cat > "$EXT_DIR/metadata.json" << 'EOF'
{
  "uuid": "cursor-tracker@cua",
  "name": "CUA Cursor Tracker",
  "description": "Exposes cursor position over D-Bus for CUA Collector",
  "shell-version": ["45", "46", "47", "48", "49"],
  "version": 1
}
EOF

cat > "$EXT_DIR/extension.js" << 'EOF'
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

const DBUS_IFACE = `
<node>
  <interface name="org.cua.CursorTracker">
    <method name="GetPosition">
      <arg type="i" direction="out" name="x"/>
      <arg type="i" direction="out" name="y"/>
    </method>
  </interface>
</node>`;

export default class CursorTrackerExtension {
    _dbusId = null;
    _ownerId = null;

    enable() {
        this._dbusId = Gio.DBus.session.register_object(
            '/org/cua/CursorTracker',
            Gio.DBusNodeInfo.new_for_xml(DBUS_IFACE).interfaces[0],
            (connection, sender, path, iface, method, params, invocation) => {
                if (method === 'GetPosition') {
                    const [x, y] = global.get_pointer();
                    invocation.return_value(new GLib.Variant('(ii)', [x, y]));
                }
            },
            null,
            null
        );

        this._ownerId = Gio.bus_own_name(
            Gio.BusType.SESSION,
            'org.cua.CursorTracker',
            Gio.BusNameOwnerFlags.NONE,
            null, null, null
        );

        console.log('[CUA] Cursor tracker extension enabled');
    }

    disable() {
        if (this._dbusId) {
            Gio.DBus.session.unregister_object(this._dbusId);
            this._dbusId = null;
        }
        if (this._ownerId) {
            Gio.bus_unown_name(this._ownerId);
            this._ownerId = null;
        }
        console.log('[CUA] Cursor tracker extension disabled');
    }
}
EOF

echo "✅ Extension files written to: $EXT_DIR"

# 2. Add to enabled-extensions
CURRENT=$(gsettings get org.gnome.shell enabled-extensions 2>/dev/null || echo "@as []")
if echo "$CURRENT" | grep -q "$EXT_UUID"; then
    echo "✅ Already in enabled-extensions"
else
    if [ "$CURRENT" = "@as []" ]; then
        NEW="['$EXT_UUID']"
    else
        NEW=$(echo "$CURRENT" | sed "s/]/, '$EXT_UUID']/")
    fi
    gsettings set org.gnome.shell enabled-extensions "$NEW"
    echo "✅ Added to enabled-extensions"
fi

echo ""
echo "⚠️  Log out and log back in for the extension to load."
echo ""
echo "   Verify after re-login:"
echo "     gdbus call --session \\"
echo "       --dest org.cua.CursorTracker \\"
echo "       --object-path /org/cua/CursorTracker \\"
echo "       --method org.cua.CursorTracker.GetPosition"
echo ""
