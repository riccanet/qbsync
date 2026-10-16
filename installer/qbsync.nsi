; QB Sync installer (NSIS 3). Built by installer/build.sh.
;
; Two install modes, chosen automatically:
;   * Run by an administrator (UAC prompt) or as SYSTEM (RMM): machine-wide install to Program Files,
;     data in %ProgramData%\QBSync, background task for the account given by /USER.
;   * Run by a standard user (no admin rights, no UAC prompt), or with /CURRENTUSER: per-user
;     install to %LOCALAPPDATA%\Programs\QB Sync, data in %LOCALAPPDATA%\QBSync, background task
;     for that user.
; Either way QB Sync itself runs with standard user rights.
;
; Interactive:  QBSync-Setup.exe
; Silent:       QBSync-Setup.exe /S /COMPANYFILE="\\QBSERVER\QBData\Acme.QBW"
;               [/WEBHOOKURL="https://n8n.example.com/webhook/quickbooks"] [/WEBHOOKSECRET=...]
;               [/USER="ACME\qbsync"] [/PASSWORD=...] [/COMPANYID=acme] [/NOTASK] [/CURRENTUSER]
;               [/D=C:\Path\Last\Without\Quotes]
;   /USER      machine-wide mode only: Windows account the background task runs as (default: the
;              installing user; required when installing as SYSTEM). Ignored in per-user mode.
;   /PASSWORD  run the task at boot whether or not anyone is logged on (default: at that user's logon).
; Upgrades keep the existing configuration and data. Re-running the installer (as admin, with /USER=
; if needed) also repairs folder permissions and recreates a missing background task.

Unicode true
!include "MUI2.nsh"
!include "nsDialogs.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"

!ifndef VERSION
  !define VERSION "0.0.0"
!endif
!define APPNAME "QB Sync"
!define TASKNAME "QBSync"
!define REGKEY "Software\QBSync"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\QBSync"
!define DEFAULT_MACHINE_DIR "$PROGRAMFILES\QB Sync"

Name "${APPNAME} ${VERSION}"
OutFile "${OUTDIR}/QBSync-Setup-${VERSION}.exe"
InstallDir "${DEFAULT_MACHINE_DIR}"
; Administrators get a UAC prompt (machine-wide install); standard users run without one (per-user).
RequestExecutionLevel highest
SetCompressor /SOLID lzma
BrandingText "${APPNAME} ${VERSION}"
ShowInstDetails show

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "${APPNAME}"
VIAddVersionKey "FileDescription" "${APPNAME} installer"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" " "

Var PerUser
Var DataDir
Var ConfigFile
Var CompanyFile
Var CompanyId
Var RunUser
Var Password
Var NoTask
Var WebhookUrl
Var WebhookSecret
Var IsUpgrade
; settings page controls
Var hCompanyFile
Var hWebhookUrl
Var hWebhookSecret
Var hTaskCheck
Var hUser

!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TEXT "This installs ${APPNAME}, which keeps a read-only copy of a QuickBooks Desktop company file and pushes changes to n8n (webhook) or Postgres.$\r$\n$\r$\nInstall it on a Windows machine that has QuickBooks Desktop installed and can open the company file. QuickBooks does not need to be running, no ports are opened, and QB Sync runs with normal user rights.$\r$\n$\r$\nQB Sync never writes to QuickBooks."
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
Page custom SettingsPage SettingsPageLeave
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_TEXT "${APPNAME} is installed.$\r$\n$\r$\nFirst-time setup: open the company file in QuickBooks as the QuickBooks Admin user on this machine, then run the connection test below and choose $\"Yes, always; allow access even if QuickBooks is not running$\" when QuickBooks asks.$\r$\n$\r$\nSettings are in $ConfigFile."
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Run the QuickBooks connection test now"
!define MUI_FINISHPAGE_RUN_FUNCTION RunConnectionTest
!define MUI_FINISHPAGE_RUN_NOTCHECKED
!define MUI_FINISHPAGE_SHOWREADME
!define MUI_FINISHPAGE_SHOWREADME_TEXT "Open the configuration file"
!define MUI_FINISHPAGE_SHOWREADME_FUNCTION OpenConfig
!define MUI_FINISHPAGE_SHOWREADME_NOTCHECKED
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; Removes one pair of surrounding double quotes: /USER="A B" arrives as "A B".
!macro StripQuotes VAR
  StrCpy $R8 ${VAR} 1
  StrCpy $R9 ${VAR} "" -1
  ${If} $R8 == '"'
  ${AndIf} $R9 == '"'
    StrCpy ${VAR} ${VAR} -1 1
  ${EndIf}
!macroend

; Removes trailing CR/LF characters (command output).
!macro TrimNewline VAR
  ${Do}
    StrCpy $R9 ${VAR} 1 -1
    ${If} $R9 == "$\r"
    ${OrIf} $R9 == "$\n"
      StrCpy ${VAR} ${VAR} -1
    ${Else}
      ${ExitDo}
    ${EndIf}
  ${Loop}
!macroend

!macro GetOpt NAME VAR
  ClearErrors
  ${GetOptions} $R0 "${NAME}" $R1
  ${IfNot} ${Errors}
    StrCpy ${VAR} $R1
    !insertmacro StripQuotes ${VAR}
  ${EndIf}
!macroend

Function .onInit
  StrCpy $NoTask "0"
  ${GetParameters} $R0
  !insertmacro GetOpt "/COMPANYFILE=" $CompanyFile
  !insertmacro GetOpt "/COMPANYID=" $CompanyId
  !insertmacro GetOpt "/USER=" $RunUser
  !insertmacro GetOpt "/PASSWORD=" $Password
  !insertmacro GetOpt "/WEBHOOKURL=" $WebhookUrl
  !insertmacro GetOpt "/WEBHOOKSECRET=" $WebhookSecret
  ClearErrors
  ${GetOptions} $R0 "/NOTASK" $R1
  ${IfNot} ${Errors}
    StrCpy $NoTask "1"
  ${EndIf}

  ; Pick the install mode.
  StrCpy $PerUser "0"
  UserInfo::GetAccountType
  Pop $0
  ${If} $0 != "Admin"
    StrCpy $PerUser "1"
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/CURRENTUSER" $R1
  ${IfNot} ${Errors}
    StrCpy $PerUser "1"
  ${EndIf}

  ReadEnvStr $0 "USERNAME"
  ReadEnvStr $1 "USERDOMAIN"
  ${If} $PerUser == "1"
    SetShellVarContext current
    StrCpy $DataDir "$LOCALAPPDATA\QBSync"
    StrCpy $RunUser "$1\$0"   ; per-user tasks always run as the installing user
  ${Else}
    SetShellVarContext all
    StrCpy $DataDir "$APPDATA\QBSync"   ; %ProgramData%\QBSync
    ${If} $RunUser == ""
      ; Default to the installing user, unless that is a machine account (RMM running as SYSTEM).
      StrCpy $RunUser "$1\$0"
      StrCpy $2 $0 1 -1
      ${If} $2 == "$$"
      ${OrIf} $0 == ""
        StrCpy $RunUser ""
      ${EndIf}
    ${EndIf}
    ${If} $RunUser == ""
      ; Running as SYSTEM without /USER: use whoever is logged on at the console.
      nsExec::ExecToStack 'powershell.exe -NoProfile -NonInteractive -Command "(Get-CimInstance Win32_ComputerSystem).UserName"'
      Pop $2
      Pop $3
      ${If} $2 == "0"
        !insertmacro TrimNewline $3
        StrCpy $RunUser $3
      ${EndIf}
    ${EndIf}
    ${If} $RunUser == ""
    ${AndIf} ${Silent}
      ; Without an account nobody but administrators could read the config or data, so fail loudly.
      SetErrorLevel 3
      Quit
    ${EndIf}
  ${EndIf}
  StrCpy $ConfigFile "$DataDir\config.yaml"

  ; Install directory: keep /D=, else reuse a previous install of this mode, else the mode's default.
  ${If} $INSTDIR == "${DEFAULT_MACHINE_DIR}"
    ReadRegStr $2 SHCTX "${REGKEY}" "InstallDir"
    ${If} $2 != ""
      StrCpy $INSTDIR $2
    ${ElseIf} $PerUser == "1"
      StrCpy $INSTDIR "$LOCALAPPDATA\Programs\QB Sync"
    ${EndIf}
  ${EndIf}

  StrCpy $IsUpgrade "0"
  ${If} ${FileExists} "$ConfigFile"
    StrCpy $IsUpgrade "1"
  ${EndIf}
FunctionEnd

Function SettingsPage
  ${If} $IsUpgrade == "1"
    Abort   ; keep the existing configuration on upgrade
  ${EndIf}
  !insertmacro MUI_HEADER_TEXT "Settings" "Which company file to sync, and where to send the data."
  nsDialogs::Create 1018
  Pop $0
  ${NSD_CreateLabel} 0 0 100% 18u "QuickBooks company file (.QBW). For a file hosted by Database Server Manager use its network path, e.g. \\QBSERVER\QBData\Company.QBW"
  Pop $0
  ${NSD_CreateText} 0 19u 82% 12u "$CompanyFile"
  Pop $hCompanyFile
  ${NSD_CreateButton} 84% 18u 16% 14u "Browse..."
  Pop $0
  ${NSD_OnClick} $0 BrowseCompanyFile

  ${NSD_CreateLabel} 0 38u 100% 10u "n8n webhook URL (optional, can be added later in the configuration file):"
  Pop $0
  ${NSD_CreateText} 0 49u 100% 12u "$WebhookUrl"
  Pop $hWebhookUrl
  ${NSD_CreateLabel} 0 65u 45% 10u "Webhook secret (blank = generate):"
  Pop $0
  ${NSD_CreatePassword} 46% 63u 54% 12u "$WebhookSecret"
  Pop $hWebhookSecret

  ${NSD_CreateLabel} 0 82u 100% 10u "Windows account that will use QB Sync (it gets access to the QB Sync data folder):"
  Pop $0
  ${NSD_CreateText} 12u 93u 60% 12u "$RunUser"
  Pop $hUser
  ${If} $PerUser == "1"
    EnableWindow $hUser 0   ; a per-user install can only schedule the current user
  ${EndIf}
  ${NSD_CreateCheckbox} 0 108u 100% 12u "Run QB Sync in the background whenever this account logs on"
  Pop $hTaskCheck
  ${If} $NoTask == "0"
    ${NSD_Check} $hTaskCheck
  ${EndIf}
  ${NSD_CreateLabel} 12u 122u 95% 18u "The account needs no admin rights, only access to the company file and QuickBooks on this machine."
  Pop $0
  nsDialogs::Show
FunctionEnd

Function BrowseCompanyFile
  ${NSD_GetText} $hCompanyFile $0
  nsDialogs::SelectFileDialog open "$0" "QuickBooks company files (*.QBW)|*.QBW|All files (*.*)|*.*"
  Pop $0
  ${If} $0 != ""
    ${NSD_SetText} $hCompanyFile $0
  ${EndIf}
FunctionEnd

Function SettingsPageLeave
  ${NSD_GetText} $hCompanyFile $CompanyFile
  ${NSD_GetText} $hWebhookUrl $WebhookUrl
  ${NSD_GetText} $hWebhookSecret $WebhookSecret
  ${NSD_GetText} $hUser $RunUser
  ${NSD_GetState} $hTaskCheck $0
  ${If} $0 == ${BST_CHECKED}
    StrCpy $NoTask "0"
  ${Else}
    StrCpy $NoTask "1"
  ${EndIf}
  ${If} $CompanyFile == ""
    MessageBox MB_ICONEXCLAMATION|MB_YESNO "No company file entered. QB Sync will not be able to sync until you set company_file in config.yaml. Continue anyway?" IDYES +2
    Abort
  ${EndIf}
  ${If} $RunUser == ""
    MessageBox MB_ICONEXCLAMATION "Enter the Windows account that will use QB Sync (for example DOMAIN\username)."
    Abort
  ${EndIf}
FunctionEnd

Section "Install"
  ${If} $PerUser == "1"
    SetShellVarContext current
  ${Else}
    SetShellVarContext all
  ${EndIf}

  ; Stop a running copy first so its files can be replaced.
  nsExec::Exec 'schtasks /End /TN "${TASKNAME}"'
  Pop $0
  Sleep 1500

  SetOutPath "$INSTDIR"
  RMDir /r "$INSTDIR\python"   ; drop files from older versions
  File /r "${STAGE}/*"

  ; Command-line wrapper that defaults to this install's config file.
  FileOpen $9 "$INSTDIR\qbsync.cmd" w
  FileWrite $9 "@echo off$\r$\nrem Runs QB Sync with the bundled Python.$\r$\nsetlocal$\r$\n"
  FileWrite $9 "if not defined QBSYNC_CONFIG set $\"QBSYNC_CONFIG=$ConfigFile$\"$\r$\n"
  FileWrite $9 "$\"%~dp0python\python.exe$\" -m qbsync %*$\r$\n"
  FileClose $9

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr SHCTX "${REGKEY}" "InstallDir" "$INSTDIR"
  WriteRegStr SHCTX "${UNINSTKEY}" "DisplayName" "${APPNAME}"
  WriteRegStr SHCTX "${UNINSTKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr SHCTX "${UNINSTKEY}" "Publisher" "QB Sync"
  WriteRegStr SHCTX "${UNINSTKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr SHCTX "${UNINSTKEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr SHCTX "${UNINSTKEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD SHCTX "${UNINSTKEY}" "NoModify" 1
  WriteRegDWORD SHCTX "${UNINSTKEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  WriteRegDWORD SHCTX "${UNINSTKEY}" "EstimatedSize" $0

  CreateDirectory "$DataDir"
  ${If} $IsUpgrade == "0"
    DetailPrint "Writing $ConfigFile"
    StrCpy $1 ""
    ${If} $CompanyId != ""
      StrCpy $1 '--company-id "$CompanyId"'
    ${EndIf}
    ${If} $WebhookUrl != ""
      StrCpy $1 '$1 --webhook-url "$WebhookUrl"'
    ${EndIf}
    ${If} $WebhookSecret != ""
      StrCpy $1 '$1 --webhook-secret "$WebhookSecret"'
    ${EndIf}
    nsExec::ExecToLog '"$INSTDIR\python\python.exe" -m qbsync -c "$ConfigFile" init --company-file "$CompanyFile" $1'
    Pop $0
    ${If} $0 != "0"
      DetailPrint "Writing the configuration failed (exit code $0)."
      SetErrorLevel 2
    ${EndIf}
  ${Else}
    DetailPrint "Keeping existing configuration $ConfigFile"
  ${EndIf}

  ${If} $PerUser == "0"
    ; Shared data folder: Administrators, SYSTEM and the run-as account only (it holds financial data
    ; and the webhook secret). Per-user installs live in the user's private profile already.
    ; Lock down the folder only, then make everything inside inherit from it, so access granted
    ; on the folder later (icacls /grant) also reaches files that already exist.
    ${If} $RunUser != ""
      nsExec::ExecToLog 'icacls "$DataDir" /inheritance:r /grant:r *S-1-5-32-544:(OI)(CI)F *S-1-5-18:(OI)(CI)F "$RunUser":(OI)(CI)M /Q'
    ${Else}
      nsExec::ExecToLog 'icacls "$DataDir" /inheritance:r /grant:r *S-1-5-32-544:(OI)(CI)F *S-1-5-18:(OI)(CI)F /Q'
    ${EndIf}
    Pop $0
    nsExec::ExecToLog 'icacls "$DataDir\*" /reset /T /C /Q'
    Pop $0
  ${EndIf}

  ; Upgrades keep an existing task (just restart it); a missing one is created as for a new install.
  StrCpy $4 "1"
  ${If} $IsUpgrade == "1"
    nsExec::Exec 'schtasks /Query /TN "${TASKNAME}"'
    Pop $4
  ${EndIf}
  ${If} $4 == "0"
    nsExec::Exec 'schtasks /Run /TN "${TASKNAME}"'
    Pop $0
  ${ElseIf} $NoTask == "0"
  ${AndIf} $RunUser != ""
    StrCpy $1 ""
    ${If} $PerUser == "0"
      StrCpy $1 '--user "$RunUser"'
    ${EndIf}
    ${If} $Password != ""
      StrCpy $1 '$1 --password "$Password"'
    ${EndIf}
    nsExec::ExecToLog '"$INSTDIR\python\python.exe" -m qbsync -c "$ConfigFile" service install $1'
    Pop $0
    ${If} $0 == "0"
      nsExec::Exec 'schtasks /Run /TN "${TASKNAME}"'
      Pop $0
    ${Else}
      DetailPrint "Creating the scheduled task failed (exit code $0). Run: qbsync service install"
    ${EndIf}
  ${Else}
    DetailPrint "No background task created. Run 'qbsync service install' later to add one."
  ${EndIf}

  ; Start Menu
  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\Test QuickBooks Connection.lnk" "$SYSDIR\cmd.exe" '/k ""$INSTDIR\qbsync.cmd" test-connection"' "$SYSDIR\shell32.dll" 23
  CreateShortcut "$SMPROGRAMS\${APPNAME}\Sync Now.lnk" "$SYSDIR\cmd.exe" '/k ""$INSTDIR\qbsync.cmd" sync"' "$SYSDIR\shell32.dll" 238
  CreateShortcut "$SMPROGRAMS\${APPNAME}\Send Test Webhook.lnk" "$SYSDIR\cmd.exe" '/k ""$INSTDIR\qbsync.cmd" test-webhook"' "$SYSDIR\shell32.dll" 13
  CreateShortcut "$SMPROGRAMS\${APPNAME}\Sync Status.lnk" "$SYSDIR\cmd.exe" '/k ""$INSTDIR\qbsync.cmd" status"' "$SYSDIR\shell32.dll" 22
  CreateShortcut "$SMPROGRAMS\${APPNAME}\QB Sync Command Prompt.lnk" "$SYSDIR\cmd.exe" '/k "cd /d "$INSTDIR" && echo Type: qbsync --help"' "$SYSDIR\cmd.exe" 0
  CreateShortcut "$SMPROGRAMS\${APPNAME}\Edit Configuration.lnk" "$WINDIR\notepad.exe" '"$ConfigFile"'
  CreateShortcut "$SMPROGRAMS\${APPNAME}\Open Data and Logs Folder.lnk" "$DataDir"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\README.lnk" "$WINDIR\notepad.exe" '"$INSTDIR\README.md"'
  CreateShortcut "$SMPROGRAMS\${APPNAME}\Uninstall ${APPNAME}.lnk" "$INSTDIR\Uninstall.exe"
SectionEnd

Function RunConnectionTest
  Exec '"$SYSDIR\cmd.exe" /k ""$INSTDIR\qbsync.cmd" test-connection"'
FunctionEnd

Function OpenConfig
  Exec '"$WINDIR\notepad.exe" "$ConfigFile"'
FunctionEnd

Function un.onInit
  ; Per-user installs live under %LOCALAPPDATA%.
  SetShellVarContext current
  StrLen $1 "$LOCALAPPDATA"
  StrCpy $2 "$INSTDIR" $1
  ${If} $2 == "$LOCALAPPDATA"
    StrCpy $PerUser "1"
    StrCpy $DataDir "$LOCALAPPDATA\QBSync"
  ${Else}
    StrCpy $PerUser "0"
    SetShellVarContext all
    StrCpy $DataDir "$APPDATA\QBSync"
  ${EndIf}
FunctionEnd

Section "Uninstall"
  nsExec::Exec 'schtasks /End /TN "${TASKNAME}"'
  Pop $0
  nsExec::Exec 'schtasks /Delete /TN "${TASKNAME}" /F'
  Pop $0
  Sleep 1500

  RMDir /r "$SMPROGRAMS\${APPNAME}"
  RMDir /r "$INSTDIR\python"
  Delete "$INSTDIR\qbsync.cmd"
  Delete "$INSTDIR\README.md"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey SHCTX "${UNINSTKEY}"
  DeleteRegKey SHCTX "${REGKEY}"

  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "Also delete the synced data, logs and configuration in $DataDir?" /SD IDNO IDNO keep
    RMDir /r "$DataDir"
  keep:
SectionEnd
