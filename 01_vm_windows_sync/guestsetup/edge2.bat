@echo off
echo === Downloading Edge MSI ===
curl -L -o C:\Users\notes\edge.msi https://msedge.sf.dl.delivery.mp.microsoft.com/filestreamingservice/files/d209ad46-3748-45ee-9a0f-826275ced29c/MicrosoftEdgeEnterpriseX64.msi
if not exist C:\Users\notes\edge.msi (echo DOWNLOAD FAILED & exit /b 1)
echo === Installing Edge ===
msiexec /i C:\Users\notes\edge.msi /quiet /norestart ALLUSERS=1 >> C:\Users\notes\edge2.log 2>&1
echo EXIT_CODE=%ERRORLEVEL% >> C:\Users\notes\edge2.log
del C:\Users\notes\edge.msi
echo === DONE ===
type C:\Users\notes\edge2.log
