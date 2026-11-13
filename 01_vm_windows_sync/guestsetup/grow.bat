@echo off
echo === EXTEND C ===
powershell -NoProfile -Command "$m=Get-Partition -DriveLetter C; $max=(Get-PartitionSupportedSize -DriveLetter C).SizeMax; Resize-Partition -DriveLetter C -Size $max; Write-Host EXTENDED" > C:\Users\notes\grow.log 2>&1
type C:\Users\notes\grow.log
echo === FREE SPACE ===
powershell -NoProfile -Command "(Get-PSDrive C).Free/1GB" >> C:\Users\notes\grow.log 2>&1
type C:\Users\notes\grow.log
echo GROW_DONE >> C:\Users\notes\grow.log
