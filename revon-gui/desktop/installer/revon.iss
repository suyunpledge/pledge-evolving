; Inno Setup script for the Revon Windows installer. Built by build-installer.ps1,
; which passes /DAppVersion, /DDistDir and /DOutputDir and generates openwith.iss.
; Per-user install (no admin): %LOCALAPPDATA%\Programs\Revon.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F2C1B7E-4D3A-4E8B-9A51-52C3E1A0B7D4}
AppName=Revon
AppVersion={#AppVersion}
AppVerName=Revon {#AppVersion}
AppPublisher=Pledge
AppPublisherURL=https://github.com/suyunpledge/pledge-revon
AppSupportURL=https://github.com/suyunpledge/pledge-revon/issues
DefaultDirName={localappdata}\Programs\Revon
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=Revon-Setup-{#AppVersion}
SetupIconFile=..\revon.ico
UninstallDisplayIcon={app}\Revon.exe
UninstallDisplayName=Revon
WizardStyle=modern
Compression=lzma2/normal
SolidCompression=yes
LicenseFile=..\..\..\LICENSE
ChangesAssociations=yes
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "openwith"; Description: "Add Revon to ""Open with"" for code and text files, and ""Open with Revon"" for folders"; GroupDescription: "Explorer:"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Source files removed in a newer version must not linger in app\.
Type: filesandordirs; Name: "{app}\app"

[Icons]
Name: "{autoprograms}\Revon"; Filename: "{app}\Revon.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Revon"; Filename: "{app}\Revon.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Classes\Applications\Revon.exe"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "Revon"; Flags: uninsdeletekey; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Applications\Revon.exe\DefaultIcon"; ValueType: string; ValueData: """{app}\Revon.exe"",0"; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Applications\Revon.exe\shell\open\command"; ValueType: string; ValueData: """{app}\Revon.exe"" ""%1"""; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Directory\shell\Revon"; ValueType: string; ValueData: "Open with Revon"; Flags: uninsdeletekey; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Directory\shell\Revon"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\Revon.exe"",0"; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Directory\shell\Revon\command"; ValueType: string; ValueData: """{app}\Revon.exe"" ""%V"""; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Directory\Background\shell\Revon"; ValueType: string; ValueData: "Open with Revon"; Flags: uninsdeletekey; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Directory\Background\shell\Revon"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\Revon.exe"",0"; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Directory\Background\shell\Revon\command"; ValueType: string; ValueData: """{app}\Revon.exe"" ""%V"""; Tasks: openwith
#include "openwith.iss"

[Run]
Filename: "{app}\Revon.exe"; Description: "Launch Revon"; Flags: nowait postinstall skipifsilent
