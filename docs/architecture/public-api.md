# Public API

The public facing API is organized around the network video recorder (NVR).

`client = DahuaClient(...)`

Everything starts here.

## Composition of the API

### Recorder Identity
```
client.model
client.manufacturer
client.oem
client.serial_number
client.hardware_revision
client.firmware_version
client.api_version
client.capabilities
client.channel_count
client.camera_count
```

### Cameras

Public camera channels are 1-based, e.g., the first camera stream is channel=1.

```
client.cameras
client.cameras.all()
client.cameras.online()
client.cameras.offline()
client.cameras.by_id(3)
client.cameras.by_name("Driveway")
```

**Each camera:**
```
camera.id
camera.name
camera.model
camera.online
camera.ip_address
camera.resolution
camera.frame_rate
```

**Operations:**
```
camera.stream.main()
camera.stream.sub()
camera.ptz.pan(...)
camera.reboot()
```

Notice that the stream belongs to the camera.

`client.cameras.live_stream(channel=1, stream="Main")` creates a live RTSP
video session. `Main` maps to Dahua subtype 0 and `Extra1` maps to subtype 1
when that RPC2 Encode profile exists. Live sessions support `start()`,
`receive(duration)`, and `close()`/context-manager cleanup.

### Media

`client.media.recordings(...)` returns indexed stored `Recording` objects.
Retrieve the stored DAV bytes with `client.media.recording_bytes(recording)`.

`client.media.snapshots(...)` returns stored `Snapshot` objects. Retrieve a
stored snapshot's JPEG with `client.media.snapshot_bytes(snapshot)`.

`client.media.playback(recording)` creates an RTSP session directly from the
indexed `Recording.file_path`. It supports `start()`, `pause()`, `resume()`,
absolute `seek(seconds)`, relative `seek_relative(delta_seconds)`, read-only
NPT `position`, synchronous RTP observation with `receive(duration)`, and
`close()`/context-manager cleanup.

RPC2 performs media indexing and search. `RPC_Loadfile` explicitly exports
indexed DAV and JPG files. RTSP performs recorded playback directly from the
recording metadata; `recording_bytes()` is not part of playback.

Camera inventory, state, and stream metadata remain RPC2-backed. Live camera
video and recorded playback share the RTSP/Digest/TCP-interleaved transport.
`RPC_Loadfile` is restricted to explicit indexed-file export.

### Storage

`client.storage.disks()`

returns

`Disk`

objects.

Then
```
disk.capacity
disk.used
disk.health
disk.temperature
```

### Events

`client.events.search(...)`

returns

`Event`

objects (not dictionaries).

### Diagnostics
```
client.diagnostics.health()
client.diagnostics.network()
client.diagnostics.cameras()
```

### Configuration
```
client.configuration.network
client.configuration.recording
client.configuration.users
```

Configuration is persistent.
