// nn::streamplay (GameShare, Switch 2's SDK): on the real device every call goes through a stpl:u session the SDK opens itself (detail::CreateUserServiceByHipc), which the host
// does not serve; Donkey Kong Bananza's start-up aborted there (Result 0xe15, sdk+0x6b6440) in GetRequiredMemorySize. The host has no GameShare: the library initializes,
// nobody ever connects, there are no guest pads, and a session cannot be opened. GuestVibrationTarget stays SDK code (it only keeps a handle under a lock).
#include "hle.h"

static _Alignas(16) uint8_t g_sp_event[0x80];   // GetConnectionStateChangedEvent's SystemEvent: bound on first use, never signaled

HLE_FORCE("_ZN2nn10streamplay21GetRequiredMemorySizeENS0_14StreamPlayTypeENS0_13StreamingModeEi", "GameShare work memory: one page (nothing streams)", uint32_t, h_sp_MemSize, int type, int mode, int n) { (void)type; (void)mode; (void)n; return 0x1000; }
HLE_FORCE("_ZN2nn10streamplay10InitializeENS0_14StreamPlayTypeENS0_13StreamingModeEPvm", "GameShare initialize: success, the memory is not used", uint32_t, h_sp_Init, int type, int mode, void *mem, size_t size) { (void)type; (void)mode; (void)mem; (void)size; return 0; }
HLE_FORCE("_ZN2nn10streamplay8FinalizeEv", "GameShare finalize", void, h_sp_Fini, void) {}
HLE_FORCE("_ZN2nn10streamplay14StartStreamingEv", "GameShare start streaming: nobody can join", void, h_sp_Start, void) {}
HLE_FORCE("_ZN2nn10streamplay12CloseSessionEv", "GameShare close session: none is open", void, h_sp_Close, void) {}
HLE_FORCE("_ZN2nn10streamplay30GetConnectionStateChangedEventEv", "GameShare connection event: bound, never signaled", void *, h_sp_ConnEvent, void) { hle_bind_system_event(g_sp_event, 0); return g_sp_event; }
HLE_FORCE("_ZN2nn10streamplay18GetConnectionStateEPNS0_15ConnectionStateE", "GameShare connection state: not connected", void, h_sp_ConnState, uint32_t *st) { *st = 0; }
// Guest pads: no guest is ever connected, so every query has nothing to report
HLE_FORCE("_ZN2nn10streamplay17GetGuestPadStatesEPNS0_13GuestPadStateINS_3hid16NpadFullKeyStateEEEiRKNS0_10GuestPadIdE", "GameShare guest full-key states: none", int, h_sp_PadFull, void *o, int n, const void *id) { (void)o; (void)n; (void)id; return 0; }
HLE_FORCE("_ZN2nn10streamplay17GetGuestPadStatesEPNS0_13GuestPadStateINS_3hid17NpadHandheldStateEEEiRKNS0_10GuestPadIdE", "GameShare guest handheld states: none", int, h_sp_PadHand, void *o, int n, const void *id) { (void)o; (void)n; (void)id; return 0; }
HLE_FORCE("_ZN2nn10streamplay17GetGuestPadStatesEPNS0_13GuestPadStateINS_3hid16NpadJoyDualStateEEEiRKNS0_10GuestPadIdE", "GameShare guest dual Joy-Con states: none", int, h_sp_PadDual, void *o, int n, const void *id) { (void)o; (void)n; (void)id; return 0; }
HLE_FORCE("_ZN2nn10streamplay17GetGuestPadStatesEPNS0_13GuestPadStateINS_3hid17NpadJoyRightStateEEEiRKNS0_10GuestPadIdE", "GameShare guest right Joy-Con states: none", int, h_sp_PadRight, void *o, int n, const void *id) { (void)o; (void)n; (void)id; return 0; }
HLE_FORCE("_ZN2nn10streamplay17GetGuestPadStatesEPNS0_13GuestPadStateINS_3hid16NpadJoyLeftStateEEEiRKNS0_10GuestPadIdE", "GameShare guest left Joy-Con states: none", int, h_sp_PadLeft, void *o, int n, const void *id) { (void)o; (void)n; (void)id; return 0; }
HLE_FORCE("_ZN2nn10streamplay29SetSupportedGuestNpadStyleSetENS_4util10BitFlagSetILi32ENS_3hid12NpadStyleTagEEE", "GameShare guest pad styles: nothing to set", void, h_sp_SetStyles, uint32_t s) { (void)s; }
HLE_FORCE("_ZN2nn10streamplay23SetGuestNpadJoyHoldTypeENS_3hid15NpadJoyHoldTypeE", "GameShare guest hold type: nothing to set", void, h_sp_SetHold, int t) { (void)t; }
HLE_FORCE("_ZN2nn10streamplay23GetGuestNpadJoyHoldTypeEv", "GameShare guest hold type: 0", int, h_sp_GetHold, void) { return 0; }
HLE_FORCE("_ZN2nn10streamplay19GetGuestPadStyleSetERKNS0_10GuestPadIdE", "GameShare guest pad style: none", uint32_t, h_sp_Style, const void *id) { (void)id; return 0; }
HLE_FORCE("_ZN2nn10streamplay28GetGuestSixAxisSensorHandlesEPNS0_24GuestSixAxisSensorHandleEiRKNS0_10GuestPadIdENS_4util10BitFlagSetILi32ENS_3hid12NpadStyleTagEEE", "GameShare guest six-axis handles: none", int, h_sp_SixH, void *o, int n, const void *id, uint32_t s) { (void)o; (void)n; (void)id; (void)s; return 0; }
HLE_FORCE("_ZN2nn10streamplay27GetGuestSixAxisSensorStatesEPNS0_23GuestSixAxisSensorStateEiRKNS0_24GuestSixAxisSensorHandleE", "GameShare guest six-axis states: none", int, h_sp_SixS, void *o, int n, const void *h) { (void)o; (void)n; (void)h; return 0; }
HLE_FORCE("_ZN2nn10streamplay30GetGuestVibrationDeviceHandlesEPNS0_26GuestVibrationDeviceHandleEiRKNS0_10GuestPadIdENS_4util10BitFlagSetILi32ENS_3hid12NpadStyleTagEEE", "GameShare guest vibration handles: none", int, h_sp_VibH, void *o, int n, const void *id, uint32_t s) { (void)o; (void)n; (void)id; (void)s; return 0; }
HLE_FORCE("_ZN2nn10streamplay27GetGuestVibrationDeviceInfoEPNS_3hid19VibrationDeviceInfoERKNS0_26GuestVibrationDeviceHandleE", "GameShare guest vibration device: unknown", void, h_sp_VibInfo, void *info, const void *h) { (void)h; memset(info, 0, 8); }
HLE_FORCE("_ZN2nn10streamplay18GetGuestJcmsHandleERKNS0_10GuestPadIdERKNS_3hid8JcmsTypeE", "GameShare guest Jcms handle: 0", uint32_t, h_sp_JcmsH, const void *id, const void *t) { (void)id; (void)t; return 0; }
HLE_FORCE("_ZN2nn10streamplay18GetGuestJcmsStatesEPNS0_14GuestJcmsStateEiRKNS0_15GuestJcmsHandleE", "GameShare guest Jcms states: none", int, h_sp_JcmsS, void *o, int n, const void *h) { (void)o; (void)n; (void)h; return 0; }
HLE_FORCE("_ZN2nn10streamplay25SetSupportedHidFeatureSetENS_4util10BitFlagSetILi32ENS0_13HidFeatureTagEEE", "GameShare HID features: nothing to set", void, h_sp_HidFeat, uint32_t s) { (void)s; }
// The SDK itself returns 0x3d1f from here without aborting when the matchmaking it waits on ends early; the game takes it as a session that did not open
HLE_FORCE("_ZN2nn10streamplay31OpenSessionWithMatchmakeSupportEPNS0_11SessionInfoERKNS0_15MatchmakeConfigE", "GameShare open session: fails (no GameShare)", uint32_t, h_sp_Open, void *info, const void *cfg) { (void)info; (void)cfg; return 0x3d1f; }
