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
; Обновление из программы передаёт /ALLUSERS, если программа стоит в Program Files.
PrivilegesRequiredOverridesAllowed=commandline
OutputDir=..\dist
OutputBaseFilename=Autosort-Setup-{#AppVersion}
SetupIconFile=autosort.ico
UninstallDisplayIcon={app}\Autosort.exe
UninstallDisplayName=Autosort
CloseApplications=yes
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
Name: "{group}\Удалить Autosort"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Autosort"; Filename: "{app}\Autosort.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Autosort.exe"; Description: "{cm:LaunchProgram,Autosort}"; Flags: nowait postinstall skipifsilent
; Обновление из программы: после тихой установки открыть новую версию.
Filename: "{app}\Autosort.exe"; Flags: nowait; Check: IsRelaunch

[Code]
const
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{6C3E2B7A-4E0B-4F6E-9C55-2B9E1A7D0A11}_is1';

function IsRelaunch: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;

function FindInstalled(var Uninstaller, Version: String): Boolean;
begin
  Result := RegQueryStringValue(HKCU, UninstallKey, 'UninstallString', Uninstaller);
  if Result then
    RegQueryStringValue(HKCU, UninstallKey, 'DisplayVersion', Version)
  else
  begin
    Result := RegQueryStringValue(HKLM64, UninstallKey, 'UninstallString', Uninstaller);
    if Result then
      RegQueryStringValue(HKLM64, UninstallKey, 'DisplayVersion', Version);
  end;
  Uninstaller := RemoveQuotes(Uninstaller);
end;

// Если Autosort уже установлен, установщик предлагает обновить его или удалить.
function InitializeSetup: Boolean;
var
  Uninstaller, Version: String;
  Code: Integer;
begin
  Result := True;
  if WizardSilent or not FindInstalled(Uninstaller, Version) then
    Exit;
  case TaskDialogMsgBox('Autosort уже установлен' + Format(' (версия %s)', [Version]),
         'Можно установить версию {#AppVersion} поверх неё (настройки и база сохранятся) или удалить программу.',
         mbConfirmation, MB_YESNOCANCEL, ['Установить {#AppVersion}', 'Удалить Autosort'], 0) of
    IDYES: Result := True;
    IDNO:
      begin
        Exec(Uninstaller, '', '', SW_SHOW, ewNoWait, Code);
        Result := False;
      end;
  else
    Result := False;
  end;
end;

// После удаления программы спрашиваем, убрать ли и данные пользователя.
// Фото и разложенные папки в OneDrive не трогаются никогда.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    if SuppressibleMsgBox(
         'Программа удалена.' + #13#10#13#10 +
         'Удалить также настройки, списки тегов, базу с тегами и журналом переносов ' +
         'и скачанную нейросеть (~450 МБ)?' + #13#10#13#10 +
         'Ваши фото и разложенные папки не затрагиваются в любом случае. ' +
         'Без базы откатить прошлые переносы будет нельзя.',
         mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
    begin
      DelTree(ExpandConstant('{localappdata}\Autosort'), True, True, True);
      DelTree(ExpandConstant('{%USERPROFILE}\.cache\huggingface\hub\models--SmilingWolf--wd-swinv2-tagger-v3'), True, True, True);
    end;
end;
