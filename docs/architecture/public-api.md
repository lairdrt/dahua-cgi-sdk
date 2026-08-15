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

### Media

`client.media.search(...)` returns stored `Recording` objects.

`client.media.snapshots(...)` returns stored `Snapshot` objects. Retrieve a
stored snapshot's JPEG with `client.media.snapshot_bytes(snapshot)`.

Recording operations:
```
recording.download()
recording.thumbnail()
recording.delete()
```

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
