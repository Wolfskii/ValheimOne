using System;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using BepInEx;
using HarmonyLib;

namespace ValheimOne.RuntimeRegression;

// Also loads beside older released DLLs: all observations use the game's public
// API, so the probe does not depend on a particular ValheimOne internal ABI.
[BepInPlugin("com.humangenome.valheimone.consoleprobe", "ValheimOne console probe", "1.0.0")]
[BepInDependency("com.humangenome.valheimone")]
public sealed class ConsoleProbe : BaseUnityPlugin
{
    private static volatile string _failure = "";
    private static int _saved;
    private static int _actions;
    private bool _started;
    private volatile bool _saving;
    private string _resultPath = "";
    private Harmony? _harmony;

    private void Awake()
    {
        if (Environment.GetEnvironmentVariable("VALHEIMONE_CONSOLE_PROBE") != "1")
        {
            enabled = false;
            return;
        }
        _resultPath = Environment.GetEnvironmentVariable("CONSOLE_PROBE_RESULT") ?? "";
        if (!Path.IsPathRooted(_resultPath)) throw new InvalidOperationException("A probe result path is required");
        _harmony = new Harmony("com.humangenome.valheimone.consoleprobe");
        _harmony.Patch(AccessTools.Method(typeof(Terminal.ConsoleCommand), "RunAction"),
            prefix: new HarmonyMethod(typeof(ConsoleProbe), nameof(CountAction)),
            finalizer: new HarmonyMethod(typeof(ConsoleProbe), nameof(CaptureFailure)));
        UnityEngine.Application.logMessageReceivedThreaded += CaptureSave;
    }

    private static void CountAction() => Interlocked.Increment(ref _actions);

    private static Exception? CaptureFailure(Exception? __exception)
    {
        if (__exception != null) _failure = __exception.ToString();
        return __exception;
    }

    private static void CaptureSave(string message, string stack, UnityEngine.LogType type)
    {
        if (message.IndexOf("World save (5/5) done", StringComparison.Ordinal) >= 0)
            Interlocked.Increment(ref _saved);
    }

    private void Update()
    {
        if (ZNet.instance != null) _saving = ZNet.instance.IsSaving();
        if (_started || ZNet.instance == null || !ZNet.instance.IsDedicated() ||
            Game.instance == null || global::Console.instance == null)
            return;
        _started = true;
        Logger.LogInfo("CONSOLE PROBE headless=" + ZNet.instance.IsDedicated() +
            " localPlayer=" + (Player.m_localPlayer != null) +
            " profile=" + (Game.instance.GetPlayerProfile() != null));
        ThreadPool.QueueUserWorkItem(_ => RunRequests());
    }

    private void RunRequests()
    {
        var result = new StringBuilder();
        bool passed = true;
        try
        {
            string root = Environment.GetEnvironmentVariable("CONSOLE_PROBE_URL") ?? "http://127.0.0.1:24583";
            if (!Uri.TryCreate(root, UriKind.Absolute, out Uri url) || !url.IsLoopback)
                throw new InvalidOperationException("Console probes require loopback");
            string token = Environment.GetEnvironmentVariable("CONSOLE_PROBE_TOKEN") ?? "";
            DateTime deadline = DateTime.UtcNow.AddSeconds(180);
            while (true)
            {
                try
                {
                    var ready = (HttpWebRequest)WebRequest.Create(root + "/api/status");
                    ready.Headers["X-LiveMap-Token"] = token;
                    ready.Timeout = 2000;
                    using (ready.GetResponse()) { }
                    break;
                }
                catch (WebException)
                {
                    if (DateTime.UtcNow >= deadline) throw;
                    Thread.Sleep(100);
                }
            }
            int settledSeconds = int.TryParse(Environment.GetEnvironmentVariable("CONSOLE_PROBE_SETTLE_SECONDS"), out int wait)
                ? Math.Max(0, Math.Min(180, wait)) : 0;
            string[] commands = { "save", "vo save", "banned", "save", "save" };
            for (int index = 0; index < commands.Length; index++)
            {
                string command = commands[index];
                if (index == 3)
                {
                    for (int i = 0; i < settledSeconds * 10; i++) Thread.Sleep(100);
                    result.AppendLine("DELAY seconds=" + settledSeconds);
                }
                _failure = "";
                int before = Volatile.Read(ref _saved);
                int actions = Volatile.Read(ref _actions);
                var request = (HttpWebRequest)WebRequest.Create(root + "/api/console/exec");
                request.Method = "POST";
                request.ContentType = "application/json";
                request.Headers["X-LiveMap-Token"] = token;
                request.Timeout = 20000;
                byte[] body = Encoding.UTF8.GetBytes("{\"command\":\"" + command + "\"}");
                using (Stream stream = request.GetRequestStream()) stream.Write(body, 0, body.Length);
                using (var response = (HttpWebResponse)request.GetResponse())
                using (var reader = new StreamReader(response.GetResponseStream()))
                {
                    string payload = reader.ReadToEnd();
                    passed &= response.StatusCode == HttpStatusCode.OK && payload.Contains("\"ok\":true");
                    result.AppendLine(command + " HTTP " + (int)response.StatusCode + " " + payload);
                }
                for (int i = 0; i < 100 && (_saving || (command != "banned" && Volatile.Read(ref _saved) == before)); i++)
                    Thread.Sleep(100);
                result.AppendLine("completedSaves=" + (Volatile.Read(ref _saved) - before) +
                    " nativeCommandInvocations=" + (Volatile.Read(ref _actions) - actions));
                result.AppendLine("exception=" + _failure);
                passed &= _failure.Length == 0 && (command == "banned" || Volatile.Read(ref _saved) > before);
                File.WriteAllText(_resultPath + ".progress", result.ToString());
            }
        }
        catch (Exception exception)
        {
            result.AppendLine("PROBE FAILURE " + exception);
            passed = false;
        }
        finally
        {
            result.AppendLine(passed ? "CONSOLE PROBE PASS" : "CONSOLE PROBE FAIL");
            File.WriteAllText(_resultPath, result.ToString());
        }
    }

    private void OnDestroy()
    {
        if (!enabled && !_started) return;
        UnityEngine.Application.logMessageReceivedThreaded -= CaptureSave;
        _harmony?.UnpatchSelf();
    }
}
