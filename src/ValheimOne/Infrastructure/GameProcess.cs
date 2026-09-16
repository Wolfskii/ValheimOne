using System;
using System.Diagnostics;
using UnityEngine;

namespace ValheimOne.Infrastructure;

// Which build of Valheim is running, decided at plugin load. ZNet does not exist yet when
// modules install their patches, so ZNet.IsDedicated is unavailable and the process itself is
// the only signal: the dedicated build ships as valheim_server(.exe/.x86_64) and is normally
// started headless. Anything else is a player's copy of the game. Neither probe is allowed to
// throw, because the answer is read while patches are being installed.
internal static class GameProcess
{
    private const string DedicatedProcessPrefix = "valheim_server";

    private static readonly Lazy<bool> DedicatedServer = new(DetectDedicatedServer);

    public static bool IsDedicatedServer => DedicatedServer.Value;

    private static bool DetectDedicatedServer()
    {
        return HasDedicatedProcessName() || IsBatchMode();
    }

    private static bool HasDedicatedProcessName()
    {
        try
        {
            using Process current = Process.GetCurrentProcess();

            // Linux truncates the process name to 15 characters, which still covers the prefix.
            return current.ProcessName.StartsWith(DedicatedProcessPrefix, StringComparison.OrdinalIgnoreCase);
        }
        catch (Exception)
        {
            return false;
        }
    }

    private static bool IsBatchMode()
    {
        try
        {
            return Application.isBatchMode;
        }
        catch (Exception)
        {
            return false;
        }
    }
}
