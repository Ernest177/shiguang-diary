# IShellLinkW/IPersistFile use Unicode paths, independent of the system code page.
if (-not ('Shiguang.DesktopShortcut' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Text;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

namespace Shiguang {
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    internal class ShellLinkObject { }

    [ComImport, Guid("000214F9-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IShellLinkW {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int count, IntPtr data, uint flags);
        void GetIDList(out IntPtr list);
        void SetIDList(IntPtr list);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int count);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string text);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int count);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string path);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int count);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string text);
        void GetHotkey(out short value);
        void SetHotkey(short value);
        void GetShowCmd(out int value);
        void SetShowCmd(int value);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int count, out int index);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
        void Resolve(IntPtr window, uint flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
    }

    public sealed class DesktopShortcut {
        public string TargetPath = "";
        public string Arguments = "";
        public string WorkingDirectory = "";
        public string Description = "";
        public int WindowStyle = 7;

        public static DesktopShortcut Load(string path) {
            DesktopShortcut result = new DesktopShortcut();
            if (!File.Exists(path)) return result;
            IShellLinkW link = (IShellLinkW)new ShellLinkObject();
            try {
                ((IPersistFile)link).Load(path, 0);
                StringBuilder text = new StringBuilder(32768);
                link.GetPath(text, text.Capacity, IntPtr.Zero, 4);
                result.TargetPath = text.ToString();
                text.Length = 0; link.GetArguments(text, text.Capacity); result.Arguments = text.ToString();
                text.Length = 0; link.GetWorkingDirectory(text, text.Capacity); result.WorkingDirectory = text.ToString();
                text.Length = 0; link.GetDescription(text, text.Capacity); result.Description = text.ToString();
                link.GetShowCmd(out result.WindowStyle);
                return result;
            } finally { Marshal.FinalReleaseComObject(link); }
        }

        public void Save(string path) {
            IShellLinkW link = (IShellLinkW)new ShellLinkObject();
            try {
                link.SetPath(TargetPath);
                link.SetArguments(Arguments);
                link.SetWorkingDirectory(WorkingDirectory);
                link.SetDescription(Description);
                link.SetShowCmd(WindowStyle);
                ((IPersistFile)link).Save(path, true);
            } finally { Marshal.FinalReleaseComObject(link); }
        }
    }
}
'@
}
