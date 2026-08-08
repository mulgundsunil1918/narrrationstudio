; Inno Setup script for Narration Studio.
;
; Built by packaging/build_windows.ps1, which passes the version and paths in.
; Produces a single NarrationStudio-Setup.exe: a normal Windows installer with
; Start-menu and optional desktop shortcuts, an uninstaller, and file
; associations for .srt and .narration.

#define AppName        "Narration Studio"
#define AppPublisher   "Sunil Mulgund"
#define AppURL         "https://github.com/mulgundsunil1918/narrrationstudio"
#define AppExeName     "NarrationStudio.bat"

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\NarrationStudio"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif
#ifndef IconFile
  #define IconFile "..\..\dist\icon\AppIcon.ico"
#endif

[Setup]
AppId={{7B3E9C42-8A15-4D6F-9E23-1C5A7F0B4D88}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=..\..\LICENSE
OutputDir={#OutputDir}
OutputBaseFilename=NarrationStudio-Setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\AppIcon.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; No admin rights needed: installs per-user, which also avoids the runtime
; being created somewhere the user cannot write to.
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "associate"; Description: "Open .srt subtitle files with {#AppName}"; GroupDescription: "File associations:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#IconFile}"; DestDir: "{app}"; DestName: "AppIcon.ico"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\AppIcon.ico"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\AppIcon.ico"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Classes\.srt\OpenWithProgids"; ValueType: string; ValueName: "NarrationStudio.srt"; ValueData: ""; Flags: uninsdeletevalue; Tasks: associate
Root: HKCU; Subkey: "Software\Classes\NarrationStudio.srt"; ValueType: string; ValueName: ""; ValueData: "Subtitle file"; Flags: uninsdeletekey; Tasks: associate
Root: HKCU; Subkey: "Software\Classes\NarrationStudio.srt\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\AppIcon.ico"; Tasks: associate
Root: HKCU; Subkey: "Software\Classes\NarrationStudio.srt\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""; Tasks: associate

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Start {#AppName}"; Flags: postinstall nowait skipifsilent shellexec

[UninstallDelete]
; The Python runtime is created next to the user's data on first launch, not
; by this installer, so remove it explicitly.
Type: filesandordirs; Name: "{localappdata}\{#AppName}"

[Code]
function PythonInstalled(): Boolean;
var
  ResultCode: Integer;
begin
  // "py -3.12" is the reliable check; fall back to python on PATH.
  Result := Exec('cmd.exe', '/C py -3.12 -c "import sys" || python -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)"',
                 '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not PythonInstalled() then
  begin
    if MsgBox('Narration Studio needs Python 3.12 or newer, which was not found.' + #13#10#13#10 +
              'You can install it now from python.org (tick "Add python.exe to PATH"), ' +
              'then run this installer again.' + #13#10#13#10 +
              'Continue installing anyway?',
              mbConfirmation, MB_YESNO) = IDNO then
      Result := False;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    // FFmpeg is a separate dependency; say so once rather than failing later.
    if not Exec('cmd.exe', '/C where ffmpeg', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
      MsgBox('Narration Studio also needs FFmpeg to fit speech to your subtitle timings.' + #13#10#13#10 +
             'Install it by running this in a terminal:' + #13#10 +
             '    winget install Gyan.FFmpeg',
             mbInformation, MB_OK);
  end;
end;
