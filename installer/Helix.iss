; Helix installer — Inno Setup script
; Produces HelixSetup.exe which installs Helix.exe into the user's
; AppData\Local\Programs\Helix folder and creates Start Menu + optional
; Desktop shortcuts. Portable Helix.exe stays available on the website
; for users who prefer no-install.

#define MyAppName       "Helix"
#define MyAppVersion    "1.1.0"
#define MyAppPublisher  "Thirdwave Corporation"
#define MyAppExeName    "Helix.exe"
#define MyAppId         "{{E3B7A9F0-5C1D-4A28-8F91-6B4E2D7C8A10}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
OutputDir=..\dist
OutputBaseFilename=HelixSetup
SetupIconFile=..\app_icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
CloseApplications=force
RestartApplications=no
VersionInfoVersion={#MyAppVersion}.0
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
AppMutex=HelixAppMutex

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "ja"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
Name: "desktopicon";    Description: "{cm:CreateDesktopIcon}";    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startuprun";     Description: "Launch Helix when I sign in to Windows"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "..\website\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}";              Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}";    Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";        Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}";        Filename: "{app}\{#MyAppExeName}"; Tasks: startuprun

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
; Note: user data in %LOCALAPPDATA%\PCHealthAgent is NOT touched by uninstall.
; Users keep scan history, settings, and memory even after reinstall.
