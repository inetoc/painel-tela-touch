param([int]$pai = 0)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -ReferencedAssemblies System.Windows.Forms -TypeDefinition @"
using System;
using System.Diagnostics;
using System.Runtime.InteropServices;
public class KB {
  delegate IntPtr LL(int n, IntPtr w, IntPtr l);
  [DllImport("user32.dll")] static extern IntPtr SetWindowsHookEx(int id, LL cb, IntPtr h, uint t);
  [DllImport("kernel32.dll")] static extern IntPtr GetModuleHandle(string n);
  [DllImport("user32.dll")] static extern IntPtr CallNextHookEx(IntPtr h, int n, IntPtr w, IntPtr l);
  static LL cb = Hook;
  static IntPtr hk = IntPtr.Zero;
  static IntPtr Hook(int n, IntPtr w, IntPtr l) {
    if (n >= 0) {
      int flags = Marshal.ReadInt32(l, 8);
      if ((flags & 0x10) == 0) return (IntPtr)1;
    }
    return CallNextHookEx(hk, n, w, l);
  }
  public static void Instalar() {
    hk = SetWindowsHookEx(13, cb, GetModuleHandle(Process.GetCurrentProcess().MainModule.ModuleName), 0);
  }
}
"@
[KB]::Instalar()
if ($pai -gt 0) {
  $timer = New-Object System.Windows.Forms.Timer
  $timer.Interval = 3000
  $timer.Add_Tick({
    if (-not (Get-Process -Id $pai -ErrorAction SilentlyContinue)) { [System.Windows.Forms.Application]::Exit() }
  })
  $timer.Start()
}
[System.Windows.Forms.Application]::Run()
