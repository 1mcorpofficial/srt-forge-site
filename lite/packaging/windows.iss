; Inno Setup: be administratoriaus teisių, diegia į %LOCALAPPDATA%\Programs
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
[Setup]
AppId={{6D0B7F4A-5A57-4E0B-9B52-5372465F4C31}
AppName=SRT Forge Lite
AppVersion={#AppVersion}
AppPublisher=1MCORP
DefaultDirName={autopf}\SRT Forge Lite
DefaultGroupName=SRT Forge Lite
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=SRT-Forge-Lite-{#AppVersion}-windows-x64
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
UninstallDisplayName=SRT Forge Lite
DisableProgramGroupPage=yes
[Languages]
Name: "lithuanian"; MessagesFile: "compiler:Languages\Lithuanian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"
[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
[Icons]
Name: "{autoprograms}\SRT Forge Lite"; Filename: "{app}\SRT Forge Lite.exe"
Name: "{autodesktop}\SRT Forge Lite"; Filename: "{app}\SRT Forge Lite.exe"; Tasks: desktopicon
[Tasks]
Name: "desktopicon"; Description: "Sukurti nuorodą darbalaukyje"; Flags: unchecked
[Run]
Filename: "{app}\SRT Forge Lite.exe"; Description: "Paleisti SRT Forge Lite"; Flags: nowait postinstall skipifsilent
