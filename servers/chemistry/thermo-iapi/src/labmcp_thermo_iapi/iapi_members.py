"""Every Thermo IAPI type and member name the pythonnet backend uses, with where it was verified.

Each key is ``"<Type>.<Member>"`` (or a fully qualified type name) exactly as it appears in the
official repository https://github.com/thermofisherlsms/iapi, checked at commit
``c246dcc8772d03c9c32e9b2fde486e97572c8fbf`` (2026-05-11). The value is the file that documents
it: the XML documentation shipped next to each interface assembly (``<member name="P:...">``
entries) or, where the XML does not list the member, an example program that uses it.

``tests/test_server.py`` parses ``pythonnet_backend.py`` and fails if the backend touches a
.NET name that is not in this table, so a typo cannot slip through to a hardware user.
Names of .NET base-class-library members (``System.TimeSpan.FromSeconds``,
``IDisposable.Dispose``, ``Assembly.LoadFrom``...) are not IAPI names and are listed separately
in :data:`BCL_MEMBERS`.
"""

from __future__ import annotations

_REPO = "https://github.com/thermofisherlsms/iapi/blob/c246dcc8772d03c9c32e9b2fde486e97572c8fbf/"

API_XML = _REPO + "lib/API-2.0.xml"
SPECTRUM_XML = _REPO + "lib/Spectrum-1.0.xml"
FACTORY_XML = _REPO + "lib/tribrid/Thermo.TNG.Factory.XML"
EXPLORIS_XML = _REPO + "lib/Exploris4.3-and-higher/Thermo.API.Exploris.NetStd-1.0.xml"
MINIFIED = _REPO + "examples/tribrid/MinifiedExample/Program.cs"
FUSION_CLIENT = _REPO + "examples/tribrid/FusionExampleClient2pt0/Form1.cs"
EXPLORIS_CONNECTION = _REPO + "examples/Exploris/3%20DataListening/Connection.cs"
EXPLORIS_DATA = _REPO + "examples/Exploris/3%20DataListening/DataReceiver.cs"
EXPLORIS_VALUES = _REPO + "examples/Exploris/7%20InstrumentValues/ValueTest.cs"
EXACTIVE_CONNECTION = (
    _REPO + "docs/exactive/2%20KeepInstrumentConnection/KeepInstrumentConnection/Connection.cs"
)

VERIFIED_IAPI_MEMBERS: dict[str, str] = {
    # --- types / namespaces imported through pythonnet -------------------------------------
    "Thermo.TNG.Factory.Factory": FACTORY_XML,  # M:Thermo.TNG.Factory.Factory`1.Create(System.Object[])
    "Factory.Create": FACTORY_XML,
    "Thermo.Interfaces.FusionAccess_V1.IFusionInstrumentAccessContainer": MINIFIED,
    "Thermo.Interfaces.ExplorisAccess_V1.IExplorisInstrumentAccessContainer": EXPLORIS_XML,
    "Thermo.Interfaces.InstrumentAccess_V1.IInstrumentAccessContainer": API_XML,
    # --- IInstrumentAccessContainer -------------------------------------------------------
    "IInstrumentAccessContainer.StartOnlineAccess": API_XML,
    "IInstrumentAccessContainer.ServiceConnected": API_XML,
    "IInstrumentAccessContainer.Get": API_XML,
    # --- IInstrumentAccess ----------------------------------------------------------------
    "IInstrumentAccess.InstrumentId": API_XML,
    "IInstrumentAccess.InstrumentName": API_XML,
    "IInstrumentAccess.Connected": API_XML,
    "IInstrumentAccess.GetMsScanContainer": API_XML,
    "IInstrumentAccess.Control": API_XML,
    "IExplorisInstrumentAccess.Licenses": EXPLORIS_XML,
    "IExplorisLicenseInfo.Features": EXPLORIS_XML,
    # --- IMsScanContainer / scans ---------------------------------------------------------
    "IMsScanContainer.DetectorClass": API_XML,
    "IMsScanContainer.MsScanArrived": API_XML,
    "MsScanEventArgs.GetScan": API_XML,
    "IMsScan.Header": API_XML,
    "IMsScan.Trailer": API_XML,
    "IMsScan.StatusLog": API_XML,
    "IInformationSourceAccess.ItemNames": SPECTRUM_XML,
    "IInformationSourceAccess.TryGetValue": SPECTRUM_XML,
    "IInformationSourceAccess.Available": SPECTRUM_XML,
    "IInformationSourceAccess.Valid": SPECTRUM_XML,
    "ISpectrum.CentroidCount": SPECTRUM_XML,
    "ISpectrum.Centroids": SPECTRUM_XML,
    "IMassIntensity.Mz": SPECTRUM_XML,
    "IMassIntensity.Intensity": SPECTRUM_XML,
    "ICentroid.Charge": SPECTRUM_XML,
    # --- IControl ---------------------------------------------------------------------------
    "IControl.Acquisition": API_XML,
    "IControl.InstrumentValues": API_XML,
    "IControl.GetScans": API_XML,
    # --- IAcquisition -------------------------------------------------------------------------
    "IAcquisition.State": API_XML,
    "IAcquisition.CanPause": API_XML,
    "IAcquisition.CanResume": API_XML,
    "IAcquisition.SetMode": API_XML,
    "IAcquisition.CreateStandbyMode": API_XML,
    "IAcquisition.Pause": API_XML,
    "IAcquisition.Resume": API_XML,
    "IAcquisition.StartAcquisition": API_XML,
    "IAcquisition.CancelAcquisition": API_XML,
    "IAcquisition.CreatePermanentAcquisition": API_XML,
    "IAcquisition.CreateAcquisitionLimitedByCount": API_XML,
    "IAcquisition.CreateAcquisitionLimitedByDuration": API_XML,
    "IState.SystemMode": API_XML,
    "IState.SystemState": API_XML,
    "IAcquisitionWorkflow.RawFileName": API_XML,
    "IAcquisitionWorkflow.SampleName": API_XML,
    "IAcquisitionWorkflow.Comment": API_XML,
    # --- instrument values (readbacks) ------------------------------------------------------
    "IInstrumentValues.ValueNames": API_XML,
    "IInstrumentValues.Get": API_XML,
    "IReadback.Content": API_XML,
    "IContent.Content": API_XML,
    "IContent.Unit": API_XML,
    "IContent.Status": API_XML,
    # --- IScans -----------------------------------------------------------------------------
    "IScans.PossibleParameters": API_XML,
    "IScans.CreateCustomScan": API_XML,
    "IScans.SetCustomScan": API_XML,
    "IScans.CancelCustomScan": API_XML,
    "IScans.CreateRepeatingScan": API_XML,
    "IScans.SetRepetitionScan": API_XML,
    "IScans.CancelRepetition": API_XML,
    "IScanDefinition.Values": API_XML,
    "IScanDefinition.RunningNumber": API_XML,
    "ICustomScan.SingleProcessingDelay": API_XML,
    "IParameterDescription.Name": API_XML,
    "IParameterDescription.Selection": API_XML,
    "IParameterDescription.DefaultValue": API_XML,
    "IParameterDescription.Help": API_XML,
}

#: Where the connection recipes come from (registry keys / XML config), per instrument family.
CONNECTION_SOURCES: dict[str, str] = {
    "tribrid": MINIFIED,
    "exploris": EXPLORIS_CONNECTION,
    "exactive": EXACTIVE_CONNECTION,
}

#: .NET base-class-library names used alongside IAPI (not part of the IAPI repository).
BCL_MEMBERS: frozenset[str] = frozenset(
    {
        "System.TimeSpan.FromSeconds",
        "System.Reflection.Assembly.LoadFrom",
        "System.Reflection.Assembly.Load",
        "Assembly.CreateInstance",
        "IDisposable.Dispose",
    }
)
