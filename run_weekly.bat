@echo off
REM iSpy weekly market sizing + Play install spikes. Scheduled Mondays 06:45.
cd /d "%~dp0"
if not exist logs mkdir logs
echo ================ WEEKLY RUN %date% %time% ================>> logs\ispy.log
"C:\Python313\python.exe" market_report.py                    >> logs\ispy.log 2>&1
"C:\Python313\python.exe" detect_spikes.py --metric installs  >> logs\ispy.log 2>&1
"C:\Python313\python.exe" notify.py                           >> logs\ispy.log 2>&1
