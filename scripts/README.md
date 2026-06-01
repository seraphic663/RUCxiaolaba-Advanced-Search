# Local Ops Scripts

## Sync Railway Runtime Data

Before using the script:

1. Install Railway CLI.
2. Run `railway login`.
3. Run `railway link` in this repository.
4. Make sure the Railway service has a Volume mounted at `/app/data`.

Sync feedback and check-in files:

```powershell
.\scripts\sync_railway_runtime.ps1 -Volume data
```

If your Railway volume is not named `data`, replace it:

```powershell
.\scripts\sync_railway_runtime.ps1 -Volume <your-volume-name>
```

The script writes timestamped archives under:

```text
data\railway_sync\YYYYMMDD-HHMMSS\
```

It also writes latest copies:

```text
data\railway_sync\feedback.latest.jsonl
data\railway_sync\checkin_count.latest.json
```

Optionally also fetch the latest Railway logs:

```powershell
.\scripts\sync_railway_runtime.ps1 -Volume data -IncludeLogs -LogLines 500
```

Logs are not synced by default because ordinary request/runtime logs are noisy
and Railway already retains them. Pull them only when debugging or before making
a release/debug snapshot.

## Schedule Hourly Sync

Register a Windows Scheduled Task that syncs every hour at minute 30:

```powershell
.\scripts\register_hourly_railway_sync.ps1
```

Check the task:

```powershell
Get-ScheduledTask -TaskName "RUCxiaolaba Railway Runtime Sync"
Get-ScheduledTaskInfo -TaskName "RUCxiaolaba Railway Runtime Sync"
```

Run it manually once:

```powershell
Start-ScheduledTask -TaskName "RUCxiaolaba Railway Runtime Sync"
```

Remove it:

```powershell
.\scripts\unregister_hourly_railway_sync.ps1
```
