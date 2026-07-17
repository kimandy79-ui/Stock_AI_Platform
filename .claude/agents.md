\# Stock AI Platform - Development Agent



Automated approval for safe development workflows: testing, scripting, version control, and diagnostics.



\## Testing \& Python



@rule acceptEdits: true

@rule bypassPermissions: \["shell"]



Covers:

\- pytest runs (any flags, any tests)

\- Python script execution (scratchpad, validation, empirical)



\## Version Control



@rule bypassPermissions: \["shell"]



Covers:

\- git diff (display or redirected)

\- git status / git log

\- git stash (push/pop)



\## File \& System Operations



@rule bypassPermissions: \["shell"]



Covers:

\- Get-\* (read-only PowerShell queries)

\- Remove-Item (temp/cache cleanup only)

\- Test-Path (file existence)

\- Measure-Object (line counts, file sizes)

\- Out-File (write to temp)

\- Select-Object (output filtering)

\- Write-Host (diagnostics)



All operations are safe, non-destructive, and confined to development/temp directories.

