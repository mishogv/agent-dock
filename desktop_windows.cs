// Windows 10 1809-22H2 shell COM layout cross-checked against
// https://github.com/MScholtes/VirtualDesktop/blob/master/VirtualDesktop.cs
// Only the widget view is pinned. Session windows are never moved or pinned.
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public static class DesktopWindows
{
    [ComImport, Guid("6D5140C1-7436-11CE-8034-00AA006009FA"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IServiceProvider { [return: MarshalAs(UnmanagedType.IUnknown)] object QueryService(ref Guid service, ref Guid iid); }
    [ComImport, Guid("372E1D3B-38D3-42E4-A15B-8AB2B178F513"), InterfaceType(ComInterfaceType.InterfaceIsIInspectable)]
    interface IView { int SetFocus(); int SwitchTo(); }
    [ComImport, Guid("1841C6D7-4F9D-42C0-AF41-8747538F10E5"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IViews {
        void GetViews(out IntPtr value); void GetViewsByZOrder(out IntPtr value); void GetViewsByAppUserModelId(string id, out IntPtr value);
        void GetViewForHwnd(IntPtr hwnd, out IView view);
    }
    [ComImport, Guid("4CE81583-1E4C-4632-A621-07A53543148F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPins {
        bool IsAppIdPinned(string id); void PinAppID(string id); void UnpinAppID(string id);
        bool IsViewPinned(IView view);
        void PinView(IView view);
        void UnpinView(IView view);
    }
    [ComImport, Guid("FF72FFDD-BE7E-43FC-9C03-AD81681E88E4"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IDesktop { bool IsViewVisible(IntPtr view); Guid GetId(); }
    [ComImport, Guid("F31574D6-B682-4CDC-BD56-1827860ABEC6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IManager {
        int GetCount(); void MoveViewToDesktop(IntPtr view, IDesktop desktop); bool CanViewMoveDesktops(IntPtr view);
        IDesktop GetCurrentDesktop(); void GetDesktops(out IntPtr desktops); [PreserveSig] int GetAdjacentDesktop(IDesktop desktop, int direction, out IDesktop adjacent);
        void SwitchDesktop(IDesktop desktop); IDesktop CreateDesktop(); void RemoveDesktop(IDesktop desktop, IDesktop fallback); IDesktop FindDesktop(ref Guid id);
    }
    [ComImport, Guid("A5CD92FF-29BE-454C-8D04-D82879FB3F1B"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPublicManager { bool IsWindowOnCurrentVirtualDesktop(IntPtr hwnd); Guid GetWindowDesktopId(IntPtr hwnd); void MoveWindowToDesktop(IntPtr hwnd, ref Guid id); }
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    struct ProcessEntry {
        public uint size, usage, pid; public UIntPtr heap; public uint module, threads, parent; public int priority; public uint flags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=260)] public string name;
    }
    [StructLayout(LayoutKind.Sequential)] struct FileTime { public uint low, high; }
    [StructLayout(LayoutKind.Sequential)] struct Point { public int x, y; }
    [StructLayout(LayoutKind.Sequential)] struct Rect { public int left, top, right, bottom; }
    delegate bool EnumProc(IntPtr hwnd, IntPtr param);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc callback, IntPtr data);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr hwnd, StringBuilder value, int size);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool ShowWindowAsync(IntPtr hwnd, int cmd);
    [DllImport("dwmapi.dll")] static extern int DwmGetWindowAttribute(IntPtr hwnd, int attribute, out int value, int size);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll", EntryPoint="GetWindowLongPtrW")] static extern IntPtr GetWindowLongPtr64(IntPtr hwnd, int index);
    [DllImport("user32.dll", EntryPoint="SetWindowLongPtrW")] static extern IntPtr SetWindowLongPtr64(IntPtr hwnd, int index, IntPtr value);
    [DllImport("user32.dll", EntryPoint="GetWindowLongW")] static extern int GetWindowLong32(IntPtr hwnd, int index);
    [DllImport("user32.dll", EntryPoint="SetWindowLongW")] static extern int SetWindowLong32(IntPtr hwnd, int index, int value);
    [DllImport("user32.dll")] static extern bool SetWindowPos(IntPtr hwnd, IntPtr after, int x, int y, int width, int height, uint flags);
    [DllImport("user32.dll")] static extern bool GetCursorPos(out Point point);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hwnd, out Rect rect);
    [DllImport("kernel32.dll")] static extern IntPtr CreateToolhelp32Snapshot(uint flags, uint pid);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] static extern bool Process32FirstW(IntPtr snapshot, ref ProcessEntry entry);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] static extern bool Process32NextW(IntPtr snapshot, ref ProcessEntry entry);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll")] static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll")] static extern bool GetProcessTimes(IntPtr handle, out FileTime created, out FileTime exited, out FileTime kernel, out FileTime user);
    static IViews views; static IPins pins; static IManager manager; static IPublicManager publicManager;
    static Dictionary<uint, ProcessEntry> processes = new Dictionary<uint, ProcessEntry>();
    static Dictionary<uint, List<IntPtr>> windows = new Dictionary<uint, List<IntPtr>>();
    public static string LastError = "";
    public static bool Ready { get { return manager != null; } }
    public static bool IsPointerInside(IntPtr hwnd) {
        Point p; Rect r;
        return IsWindow(hwnd) && GetCursorPos(out p) && GetWindowRect(hwnd, out r) && p.x >= r.left && p.x < r.right && p.y >= r.top && p.y < r.bottom;
    }
    static long ExtendedStyle(IntPtr hwnd) { return IntPtr.Size == 8 ? GetWindowLongPtr64(hwnd, -20).ToInt64() : GetWindowLong32(hwnd, -20); }
    public static bool HiddenFromSwitcher(IntPtr hwnd) { return IsWindow(hwnd) && (ExtendedStyle(hwnd) & 0x80) != 0 && (ExtendedStyle(hwnd) & 0x40000) == 0; }
    public static bool HideFromSwitcher(IntPtr hwnd) {
        if (!IsWindow(hwnd)) return false;
        // Preserve layered/topmost/click behavior; only change shell enumeration.
        long style = (ExtendedStyle(hwnd) | 0x80L) & ~0x40000L; // TOOLWINDOW, not APPWINDOW
        if (IntPtr.Size == 8) SetWindowLongPtr64(hwnd, -20, new IntPtr(style)); else SetWindowLong32(hwnd, -20, (int)style);
        SetWindowPos(hwnd, IntPtr.Zero, 0, 0, 0, 0, 0x37); // no move/size/z-order/activation; frame changed
        return HiddenFromSwitcher(hwnd);
    }
    static object Service(IServiceProvider shell, string service, Type type) { Guid sid = new Guid(service), iid = type.GUID; return shell.QueryService(ref sid, ref iid); }
    public static bool Initialize() {
        try {
            // Never call an undocumented vtable on a different Windows family.
            int build = int.Parse((string)Microsoft.Win32.Registry.GetValue(@"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows NT\CurrentVersion", "CurrentBuildNumber", "0"));
            if (build < 17763 || build > 19045) { LastError = "Unsupported Windows build for desktop pinning"; return false; }
            var shell = (IServiceProvider)Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("C2F03A33-21F5-47FA-B4BB-156362A2F239")));
            views = (IViews)Service(shell, "1841C6D7-4F9D-42C0-AF41-8747538F10E5", typeof(IViews));
            pins = (IPins)Service(shell, "B5A399E7-1C87-46B8-88E9-FC5747B171BD", typeof(IPins));
            manager = (IManager)Service(shell, "C5E0CDCA-7B6E-41B2-9FC4-D93975CC467B", typeof(IManager));
            publicManager = (IPublicManager)Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("AA509086-5CA9-4C25-8F95-589D3C07B48A")));
            LastError = ""; return true;
        } catch (Exception e) { LastError = e.Message; manager = null; return false; }
    }
    public static int DesktopCount() { try { return Ready ? manager.GetCount() : 0; } catch { return 0; } }
    public static Guid DesktopId(IntPtr hwnd) { try { return publicManager.GetWindowDesktopId(hwnd); } catch { return Guid.Empty; } }
    public static Guid CurrentDesktopId() { try { IDesktop d = manager.GetCurrentDesktop(); try { return d.GetId(); } finally { Marshal.ReleaseComObject(d); } } catch { return Guid.Empty; } }
    public static int DesktopNumber(IntPtr hwnd) {
        try {
            Guid id = DesktopId(hwnd);
            byte[] ids = Microsoft.Win32.Registry.GetValue(@"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Explorer\VirtualDesktops", "VirtualDesktopIDs", null) as byte[];
            if (ids != null) for (int i = 0; i + 16 <= ids.Length; i += 16) { byte[] part = new byte[16]; Array.Copy(ids, i, part, 0, 16); if (new Guid(part) == id) return i / 16 + 1; }
        } catch { }
        return 0;
    }
    public static bool IsCurrent(IntPtr hwnd) { try { return publicManager.IsWindowOnCurrentVirtualDesktop(hwnd); } catch { return false; } }
    public static bool Pin(IntPtr hwnd) {
        IView view = null;
        try { if (!Ready && !Initialize()) return false; views.GetViewForHwnd(hwnd, out view); if (!pins.IsViewPinned(view)) pins.PinView(view); return pins.IsViewPinned(view); }
        catch (Exception e) { LastError = e.Message; return false; }
        finally { if (view != null) Marshal.ReleaseComObject(view); }
    }
    public static bool IsPinned(IntPtr hwnd) {
        IView view = null;
        try { if (!Ready) return false; views.GetViewForHwnd(hwnd, out view); return pins.IsViewPinned(view); } catch { return false; }
        finally { if (view != null) Marshal.ReleaseComObject(view); }
    }
    public static void RefreshWindows() {
        var next = new Dictionary<uint, ProcessEntry>();
        IntPtr snapshot = CreateToolhelp32Snapshot(2, 0);
        if (snapshot != new IntPtr(-1)) {
            try { var entry = new ProcessEntry(); entry.size = (uint)Marshal.SizeOf(entry); if (Process32FirstW(snapshot, ref entry)) do { next[entry.pid] = entry; } while (Process32NextW(snapshot, ref entry)); }
            finally { CloseHandle(snapshot); }
        }
        processes = next; windows = new Dictionary<uint, List<IntPtr>>();
        EnumWindows(delegate(IntPtr hwnd, IntPtr _) {
            if (!IsWindowVisible(hwnd)) return true;
            var cls = new StringBuilder(256); GetClassName(hwnd, cls, cls.Capacity);
            // Never treat the desktop/taskbar or helper windows as a session target.
            string name = cls.ToString(); if (name != "ConsoleWindowClass" && name != "CASCADIA_HOSTING_WINDOW_CLASS" && name != "VirtualConsoleClass") return true;
            uint pid; GetWindowThreadProcessId(hwnd, out pid);
            if (!windows.ContainsKey(pid)) windows[pid] = new List<IntPtr>(); windows[pid].Add(hwnd); return true;
        }, IntPtr.Zero);
    }
    public static long ResolveWindow(int sessionPid) {
        uint pid = (uint)sessionPid; var seen = new HashSet<uint>();
        for (int depth = 0; depth < 16 && pid > 0 && seen.Add(pid); depth++) {
            ProcessEntry p; if (!processes.TryGetValue(pid, out p) || p.name.Equals("explorer.exe", StringComparison.OrdinalIgnoreCase)) break;
            var candidates = new HashSet<IntPtr>();
            if (windows.ContainsKey(pid)) foreach (IntPtr h in windows[pid]) candidates.Add(h);
            foreach (var child in processes.Values) {
                if (child.parent == pid && child.name.Equals("conhost.exe", StringComparison.OrdinalIgnoreCase) && windows.ContainsKey(child.pid))
                    foreach (IntPtr h in windows[child.pid]) candidates.Add(h);
            }
            if (candidates.Count == 1) { foreach (IntPtr h in candidates) return h.ToInt64(); }
            if (candidates.Count > 1) return 0; // Ambiguous terminal/window: never guess.
            pid = p.parent;
        }
        return 0;
    }
    public static string ProcessStarted(int pid) {
        IntPtr handle = OpenProcess(0x1000, false, (uint)pid); if (handle == IntPtr.Zero) return "";
        try { FileTime a,b,c,d; return GetProcessTimes(handle,out a,out b,out c,out d) ? (((ulong)a.high << 32) | a.low).ToString() : ""; }
        finally { CloseHandle(handle); }
    }
    public static bool ActivateSession(int pid, string expectedStarted, long expectedWindow) {
        if (String.IsNullOrEmpty(expectedStarted) || ProcessStarted(pid) != expectedStarted) { LastError = "Session process changed"; return false; }
        RefreshWindows(); long window = ResolveWindow(pid);
        if (window == 0 || window != expectedWindow) { LastError = "Session window changed"; return false; }
        return Activate(new IntPtr(window));
    }
    public static bool Activate(IntPtr hwnd) {
        try {
            if (!Ready || !IsWindow(hwnd)) return false;
            Guid id = publicManager.GetWindowDesktopId(hwnd);
            if (!publicManager.IsWindowOnCurrentVirtualDesktop(hwnd)) {
                IDesktop target = manager.FindDesktop(ref id); try { manager.SwitchDesktop(target); } finally { Marshal.ReleaseComObject(target); }
            }
            if (IsIconic(hwnd)) ShowWindowAsync(hwnd, 9);
            // Desktop animation is asynchronous. Do not focus a still-cloaked window.
            for (int wait = 0; wait < 28; wait++) { int cloaked; if (DwmGetWindowAttribute(hwnd, 14, out cloaked, 4) != 0 || cloaked == 0) break; System.Threading.Thread.Sleep(25); }
            IView view = null;
            try { views.GetViewForHwnd(hwnd, out view); view.SwitchTo(); }
            catch (COMException) { }
            finally { if (view != null) Marshal.ReleaseComObject(view); }
            bool focused = SetForegroundWindow(hwnd);
            focused = focused || GetForegroundWindow() == hwnd;
            LastError = "";
            if (!focused) LastError = "Windows did not grant foreground focus";
            return focused;
        } catch (Exception e) { LastError = e.Message; return false; }
    }
}
