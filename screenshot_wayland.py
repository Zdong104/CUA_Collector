"""
Wayland Screenshot via PipeWire Screencast Portal.

Flow:
1. A helper GJS process sets up the screencast session + keeps PipeWire fd open
2. User approves screen share once via GNOME dialog
3. For each screenshot, the Python process sends a command to the GJS helper
4. GJS uses GStreamer to capture one frame and saves it to the requested path
5. No more dialogs needed for the rest of the session!
"""
import os
import sys
import json
import time
import shutil
import subprocess
import threading
from pathlib import Path

# The GJS script:
# - Sets up screencast session via XDG portal
# - Keeps the session alive
# - Listens on stdin for "capture <path>" commands
# - Uses GStreamer to grab frames from the PipeWire stream
GJS_SCREENCAST_SCRIPT = r'''
const { Gio, GLib, GObject } = imports.gi;

// Import GStreamer
imports.gi.versions.Gst = '1.0';
const Gst = imports.gi.Gst;
Gst.init(null);

let loop = new GLib.MainLoop(null, false);
let bus = Gio.bus_get_sync(Gio.BusType.SESSION, null);

let portal = Gio.DBusProxy.new_for_bus_sync(
    Gio.BusType.SESSION,
    Gio.DBusProxyFlags.NONE,
    null,
    'org.freedesktop.portal.Desktop',
    '/org/freedesktop/portal/desktop',
    'org.freedesktop.portal.ScreenCast',
    null
);

function callPortal(method, args, timeout) {
    return portal.call_sync(method, args,
        Gio.DBusCallFlags.NONE, timeout || 30000, null);
}

function waitForResponse(requestPath) {
    return new Promise((resolve, reject) => {
        let timeoutId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 60, () => {
            reject(new Error('Timeout waiting for portal response'));
            return false;
        });

        bus.signal_subscribe(
            'org.freedesktop.portal.Desktop',
            'org.freedesktop.portal.Request',
            'Response',
            requestPath,
            null,
            Gio.DBusSignalFlags.NONE,
            (conn, sender, path, iface, signal_name, params) => {
                GLib.source_remove(timeoutId);
                let response = params.get_child_value(0).get_uint32();
                let results = params.get_child_value(1);
                resolve({response, results});
            }
        );
    });
}

let pwFd = -1;
let pwNodeId = -1;

function captureFrame(outputPath) {
    try {
        // Build GStreamer pipeline to capture ONE frame
        let pipelineStr;
        if (pwFd >= 0) {
            pipelineStr = `pipewiresrc fd=${pwFd} path=${pwNodeId} num-buffers=1 do-timestamp=true keepalive-time=1000 always-copy=true ! videoconvert ! pngenc ! filesink location=${outputPath}`;
        } else {
            pipelineStr = `pipewiresrc path=${pwNodeId} num-buffers=1 do-timestamp=true keepalive-time=1000 always-copy=true ! videoconvert ! pngenc ! filesink location=${outputPath}`;
        }

        let pipeline = Gst.parse_launch(pipelineStr);
        pipeline.set_state(Gst.State.PLAYING);

        let gstBus = pipeline.get_bus();
        // Wait for EOS or error (max 10 seconds)
        let msg = gstBus.timed_pop_filtered(10 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR);

        if (msg !== null && msg.type === Gst.MessageType.ERROR) {
            let [err, debug] = msg.parse_error();
            pipeline.set_state(Gst.State.NULL);
            return JSON.stringify({error: `GStreamer error: ${err.message}`});
        }

        pipeline.set_state(Gst.State.NULL);

        // Verify file was created
        let file = Gio.File.new_for_path(outputPath);
        if (file.query_exists(null)) {
            return JSON.stringify({success: true, path: outputPath});
        } else {
            return JSON.stringify({error: 'Output file not created'});
        }
    } catch(e) {
        return JSON.stringify({error: e.message});
    }
}

async function main() {
    try {
        // 1. Create session
        let sessionResult = callPortal('CreateSession',
            new GLib.Variant('(a{sv})', [{'session_handle_token': new GLib.Variant('s', 'cua_session'),
                                          'handle_token': new GLib.Variant('s', 'cua_create')}]),
            30000
        );

        let createReqPath = sessionResult.get_child_value(0).get_string()[0];
        let createResp = await waitForResponse(createReqPath);

        if (createResp.response !== 0) {
            print(JSON.stringify({error: 'CreateSession denied', code: createResp.response}));
            loop.quit();
            return;
        }

        let sessionHandle = createResp.results.lookup_value('session_handle', GLib.VariantType.new('s')).get_string()[0];

        // 2. Select sources (entire monitor)
        let selectResult = callPortal('SelectSources',
            new GLib.Variant('(oa{sv})', [sessionHandle, {
                'handle_token': new GLib.Variant('s', 'cua_select'),
                'types': new GLib.Variant('u', 1),  // 1=monitor
                'multiple': new GLib.Variant('b', false),
            }]),
            30000
        );

        let selectReqPath = selectResult.get_child_value(0).get_string()[0];
        let selectResp = await waitForResponse(selectReqPath);

        if (selectResp.response !== 0) {
            print(JSON.stringify({error: 'SelectSources denied', code: selectResp.response}));
            loop.quit();
            return;
        }

        // 3. Start the stream
        let startResult = callPortal('Start',
            new GLib.Variant('(osa{sv})', [sessionHandle, '', {
                'handle_token': new GLib.Variant('s', 'cua_start'),
            }]),
            60000
        );

        let startReqPath = startResult.get_child_value(0).get_string()[0];
        let startResp = await waitForResponse(startReqPath);

        if (startResp.response !== 0) {
            print(JSON.stringify({error: 'Start denied', code: startResp.response}));
            loop.quit();
            return;
        }

        // Extract PipeWire node ID
        let streamsVariant = startResp.results.lookup_value('streams', null);
        if (!streamsVariant || streamsVariant.n_children() === 0) {
            print(JSON.stringify({error: 'No streams in response'}));
            loop.quit();
            return;
        }

        let stream = streamsVariant.get_child_value(0);
        pwNodeId = stream.get_child_value(0).get_uint32();

        // Get the PipeWire fd
        try {
            let fdResult = portal.call_with_unix_fd_list_sync(
                'OpenPipeWireRemote',
                new GLib.Variant('(oa{sv})', [sessionHandle, {}]),
                Gio.DBusCallFlags.NONE,
                30000,
                null,
                null
            );
            let fdList = fdResult[1];
            let fdIndex = fdResult[0].get_child_value(0).get_handle();
            pwFd = fdList.get(fdIndex);
        } catch(e) {
            // Some portals don't support OpenPipeWireRemote, use without fd
            pwFd = -1;
        }

        // Report success
        print(JSON.stringify({
            ready: true,
            node_id: pwNodeId,
            pw_fd: pwFd,
        }));

        // Now listen on stdin for capture commands
        let stdin = Gio.DataInputStream.new(
            new Gio.UnixInputStream({fd: 0, close_fd: false})
        );

        function readNextCommand() {
            stdin.read_line_async(GLib.PRIORITY_DEFAULT, null, (source, res) => {
                try {
                    let [line] = source.read_line_utf8_finish(res);
                    if (line === null) {
                        // EOF - parent process closed stdin
                        loop.quit();
                        return;
                    }
                    line = line.trim();
                    if (line === 'quit') {
                        loop.quit();
                        return;
                    }
                    if (line.startsWith('capture ')) {
                        let path = line.substring(8).trim();
                        let result = captureFrame(path);
                        print(result);
                    }
                    // Read next command
                    readNextCommand();
                } catch(e) {
                    print(JSON.stringify({error: `stdin read error: ${e.message}`}));
                    loop.quit();
                }
            });
        }

        readNextCommand();
        loop.run();

    } catch(e) {
        print(JSON.stringify({error: e.message}));
        loop.quit();
    }
}

main();
'''


class PipeWireScreenshotter:
    """Take screenshots via PipeWire screencast on Wayland GNOME."""

    def __init__(self):
        self._session_proc = None
        self._node_id = None
        self._ready = False
        self._lock = threading.Lock()

    def start_session(self) -> bool:
        """Start a PipeWire screencast session. Shows a one-time share dialog."""
        print("  🖥️  Requesting screen share (one-time approval)...")
        print("  📢 Please approve the screen share dialog that appears.")

        # Write the gjs script to a temp file
        script_path = '/tmp/cua_screencast.js'
        with open(script_path, 'w') as f:
            f.write(GJS_SCREENCAST_SCRIPT)

        self._session_proc = subprocess.Popen(
            ['gjs', script_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Read the JSON response (blocks until user approves or error)
        try:
            line = self._session_proc.stdout.readline()
            if not line:
                err = self._session_proc.stderr.read()
                print(f"  ❌ Screencast session failed: {err[:500]}")
                return False

            data = json.loads(line.strip())

            if 'error' in data:
                print(f"  ❌ Screencast error: {data['error']}")
                return False

            if data.get('ready'):
                self._node_id = data['node_id']
                self._ready = True
                print(f"  ✅ Screen share active! PipeWire node: {self._node_id}")
                return True

            print(f"  ❌ Unexpected response: {data}")
            return False

        except (json.JSONDecodeError, KeyError, IndexError) as e:
            print(f"  ❌ Failed to parse screencast response: {e}")
            try:
                stderr = self._session_proc.stderr.read()
                if stderr:
                    print(f"  stderr: {stderr[:500]}")
            except Exception:
                pass
            return False

    def capture(self, output_path: str) -> bool:
        """Capture a single frame from the PipeWire stream."""
        if not self._ready or not self._session_proc:
            print("  ❌ PipeWire session not ready!")
            return False

        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        with self._lock:
            try:
                # Send capture command to the GJS helper
                self._session_proc.stdin.write(f'capture {output_path}\n')
                self._session_proc.stdin.flush()

                # Read the response
                line = self._session_proc.stdout.readline()
                if not line:
                    print("  ❌ GJS helper stopped responding")
                    self._ready = False
                    return False

                result = json.loads(line.strip())
                if result.get('success'):
                    return True
                else:
                    print(f"  ❌ Capture failed: {result.get('error', 'unknown')}")
                    return False

            except Exception as e:
                print(f"  ❌ Capture error: {e}")
                return False

    def stop(self):
        """Stop the screencast session."""
        if self._session_proc:
            try:
                self._session_proc.stdin.write('quit\n')
                self._session_proc.stdin.flush()
                self._session_proc.wait(timeout=3)
            except Exception:
                try:
                    self._session_proc.terminate()
                    self._session_proc.wait(timeout=2)
                except Exception:
                    try:
                        self._session_proc.kill()
                    except Exception:
                        pass
        self._ready = False


class InteractivePortalScreenshotter:
    """Fallback: use XDG portal with interactive mode (shows dialog each time)."""

    def capture(self, output_path: str) -> bool:
        """This will show the GNOME screenshot dialog each time."""
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        gjs_cmd = '''
const { Gio, GLib } = imports.gi;
let loop = new GLib.MainLoop(null, false);
let bus = Gio.bus_get_sync(Gio.BusType.SESSION, null);
let portal = Gio.DBusProxy.new_for_bus_sync(
    Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, null,
    'org.freedesktop.portal.Desktop',
    '/org/freedesktop/portal/desktop',
    'org.freedesktop.portal.Screenshot', null
);
let result = portal.call_sync('Screenshot',
    new GLib.Variant('(sa{sv})', ['', {'interactive': new GLib.Variant('b', true)}]),
    Gio.DBusCallFlags.NONE, 30000, null);
let requestPath = result.get_child_value(0).get_string()[0];
bus.signal_subscribe('org.freedesktop.portal.Desktop',
    'org.freedesktop.portal.Request', 'Response', requestPath, null,
    Gio.DBusSignalFlags.NONE,
    (conn, sender, path, iface, signal, params) => {
        let response = params.get_child_value(0).get_uint32();
        if (response === 0) {
            let uri = params.get_child_value(1).lookup_value('uri', GLib.VariantType.new('s'));
            if (uri) {
                let file = Gio.File.new_for_uri(uri.get_string()[0]);
                print(file.get_path()); // 直接输出干净的绝对路径，比如 /home/user/Pictures/1.png
            }
        }
        loop.quit();
    }
);
GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 30, () => { loop.quit(); return false; });
loop.run();
'''
        try:
            result = subprocess.run(
                ['gjs', '-c', gjs_cmd],
                capture_output=True, text=True, timeout=35,
            )
            if result.returncode == 0 and result.stdout.strip():
                uri = result.stdout.strip()
                src = uri.replace('file://', '')
                shutil.copy2(src, output_path)
                return True
        except Exception as e:
            print(f"  ❌ Interactive screenshot failed: {e}")
        return False

    def stop(self):
        pass


def create_wayland_screenshotter():
    """Create the best available Wayland screenshotter."""
    pw = PipeWireScreenshotter()
    if pw.start_session():
        return pw

    print("  ⚠️  PipeWire screencast failed. Falling back to interactive portal.")
    return InteractivePortalScreenshotter()
