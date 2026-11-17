; Установщик Inno Setup: ISCC packaging\installer.iss (версия — из переменной APP_VERSION)
#define AppVersion GetEnv("APP_VERSION")
#if AppVersion == ""
  #define AppVersion "dev"
#endif

[Setup]
AppId={{6C3E2B7A-4E0B-4F6E-9C55-2B9E1A7D0A11}
AppName=Autosort
AppVersion={#AppVersion}
AppPublisher=LonelyAsya
AppPublisherURL=https://github.com/LonelyAsya/autosort
DefaultDirName={localappdata}\Programs\Autosort
DefaultGroupName=Autosort
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Autosort-Setup-{#AppVersion}
SetupIconFile=autosort.ico
UninstallDisplayIcon={app}\Autosort.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\Autosort\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Autosort"; Filename: "{app}\Autosort.exe"
Name: "{autodesktop}\Autosort"; Filename: "{app}\Autosort.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Autosort.exe"; Description: "{cm:LaunchProgram,Autosort}"; Flags: nowait postinstall skipifsilent
