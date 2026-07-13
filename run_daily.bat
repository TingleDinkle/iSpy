@echo off
REM iSpy daily collection chain. Run automatically by Task Scheduler at 06:00,
REM or double-click it any time to run manually. Output goes to logs\ispy.log.
cd /d "%~dp0"
if not exist logs mkdir logs
echo ================ DAILY RUN %date% %time% ================>> logs\ispy.log
"C:\Python313\python.exe" daily_snapshot.py  >> logs\ispy.log 2>&1
"C:\Python313\python.exe" detect_events.py   >> logs\ispy.log 2>&1
"C:\Python313\python.exe" analyze_reviews.py >> logs\ispy.log 2>&1
"C:\Python313\python.exe" detect_spikes.py   >> logs\ispy.log 2>&1
"C:\Python313\python.exe" notify.py          >> logs\ispy.log 2>&1
