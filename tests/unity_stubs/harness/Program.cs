// Drives plugins/unity end to end without Unity: the real plug-in code, the real
// gate, and the stand-in UnityEditor from ../UnityStubs.cs. Run by
// tests/test_engine_plugins.py, which builds the project folder and checks the
// result printed on the last line (AGK_HARNESS_RESULT {...}).
//
//     dotnet run --project tests/unity_stubs/harness -- <project> <python> <gate folder> <slow gate folder> <logging gate folder>
//
// The logging gate is the real gate behind a wrapper that appends its arguments to
// $AGK_HARNESS_GATE_LOG, so the harness can see how many runs a batch took.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Threading;
using AssetGatekeeper.Editor;
using UnityEditor;
using UnityEngine;

static class Harness
{
    static readonly Dictionary<string, object> Result = new Dictionary<string, object>();

    static void Pump(int timeoutSeconds)
    {
        var clock = Stopwatch.StartNew();
        while (clock.Elapsed.TotalSeconds < timeoutSeconds)
        {
            EditorApplication.Tick();
            if (!AgkChecker.Busy)
            {
                Thread.Sleep(50);
                EditorApplication.Tick();
                if (!AgkChecker.Busy) return;
            }
            Thread.Sleep(50);
        }
        throw new TimeoutException("the plug-in did not finish in " + timeoutSeconds + " s");
    }

    static void Import(params string[] assetPaths)
    {
        MethodInfo hook = typeof(AgkPostprocessor).GetMethod("OnPostprocessAllAssets",
            BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public);
        hook.Invoke(null, new object[] { assetPaths, new string[0], new string[0], new string[0] });
    }

    static void Menu(string method)
    {
        typeof(AgkMenu).GetMethod(method, BindingFlags.Static | BindingFlags.NonPublic).Invoke(null, null);
    }

    static List<string> TakeLines()
    {
        lock (UnityEngine.Debug.Lines)
        {
            var lines = new List<string>(UnityEngine.Debug.Lines);
            UnityEngine.Debug.Lines.Clear();
            UnityEngine.Debug.Contexts.Clear();
            return lines;
        }
    }

    static Dictionary<string, string[]> Labels()
    {
        return AssetDatabase.Labels.ToDictionary(pair => pair.Key, pair => pair.Value);
    }

    static string gateLog = "";

    /// <summary>The argument lists the logging gate was started with since the last call.</summary>
    static List<List<string>> TakeRuns()
    {
        var runs = new List<List<string>>();
        if (File.Exists(gateLog))
        {
            foreach (string line in File.ReadAllLines(gateLog))
                if (line.Trim().Length > 0) runs.Add(JsonSerializer.Deserialize<List<string>>(line));
            File.Delete(gateLog);
        }
        return runs;
    }

    static Dictionary<string, object> Describe(GateResult result)
    {
        return new Dictionary<string, object>
        {
            { "outcome", result.Outcome.ToString() }, { "status", result.Status },
            { "problems", result.Problems }, { "reason", result.Reason },
        };
    }

    static int Main(string[] args)
    {
        string project = Path.GetFullPath(args[0]);
        string python = args[1];
        string gateHome = args[2];
        string slowGateHome = args[3];
        string loggingGateHome = args[4];
        gateLog = Path.Combine(project, "gate_runs.log");
        Environment.SetEnvironmentVariable("AGK_HARNESS_GATE_LOG", gateLog);

        Application.dataPath = Path.Combine(project, "Assets");
        EditorPrefs.SetString("AssetGatekeeper.Python", python);
        EditorPrefs.SetString("AssetGatekeeper.GateHome", loggingGateHome);
        // An editor started from another DCC: the gate must not inherit its PYTHONHOME.
        string bogusHome = Path.Combine(project, "someone_elses_python");
        Directory.CreateDirectory(bogusHome);
        Environment.SetEnvironmentVariable("PYTHONHOME", bogusHome);
        RuntimeHelpers.RunClassConstructor(typeof(AgkChecker).TypeHandle);

        // 1. An import of two models, two textures and a text file.
        var imported = new[]
        {
            "Assets/Models/good.fbx", "Assets/Models/heavy.fbx",
            "Assets/My Textures/brick_Albedo.png", "Assets/My Textures/héros_odd.png",
            "Assets/readme.txt", "Packages/com.other/x.fbx",
        };
        Import(imported);
        Pump(300);
        Result["import_lines"] = TakeLines();
        Result["import_labels"] = Labels();
        Result["finish_status"] = Progress.LastFinish.ToString();
        Result["import_runs"] = TakeRuns();

        // 2. The same import again (Unity re-imports when a label rewrites the .meta).
        int setLabels = AssetDatabase.SetLabelsCalls;
        Import(imported);
        Result["reimport_busy"] = AgkChecker.Busy;
        Pump(60);
        Result["reimport_lines"] = TakeLines();
        Result["reimport_set_labels"] = AssetDatabase.SetLabelsCalls - setLabels;

        // 2b. A batch with a file that is gone: forced, as Check Selected queues them.
        AgkChecker.Enqueue(new[]
        {
            "Assets/Models/good.fbx", "Assets/Models/heavy.fbx", "Assets/Models/gone.fbx",
            "Assets/My Textures/brick_Albedo.png", "Assets/My Textures/héros_odd.png",
        }, true, true);
        Pump(300);
        Result["batch_lines"] = TakeLines();
        Result["batch_labels"] = Labels();
        Result["batch_runs"] = TakeRuns();

        // 3. The heavy model fixed on disk, then Check Selected on the Models folder.
        File.Copy(Path.Combine(project, "Assets/Models/good.fbx"), Path.Combine(project, "Assets/Models/heavy.fbx"), true);
        Selection.assetGUIDs = new[] { "Assets/Models" };
        Menu("CheckSelected");
        Pump(300);
        Result["fixed_lines"] = TakeLines();
        Result["fixed_labels"] = Labels();
        TakeRuns();

        // 4. Check All with a Python that does not exist: NOT checked, said once, labels kept.
        EditorPrefs.SetString("AssetGatekeeper.Python", Path.Combine(project, "no", "python"));
        Menu("CheckAll");
        Pump(60);
        Result["missing_python_lines"] = TakeLines();
        Result["missing_python_labels"] = Labels();

        // 5. A gate that hangs is stopped at the timeout.
        EditorPrefs.SetString("AssetGatekeeper.Python", python);
        EditorPrefs.SetString("AssetGatekeeper.GateHome", slowGateHome);
        EditorPrefs.SetInt("AssetGatekeeper.TimeoutSeconds", 5);
        var clock = Stopwatch.StartNew();
        Selection.assetGUIDs = new[] { "Assets/Models/good.fbx" };
        Menu("CheckSelected");
        Pump(60);
        Result["timeout_seconds"] = clock.Elapsed.TotalSeconds;
        Result["timeout_lines"] = TakeLines();

        // 6. Turned off: imports are not checked.
        EditorPrefs.SetBool("AssetGatekeeper.Enabled", false);
        Import("Assets/Models/good.fbx");
        Result["disabled_busy"] = AgkChecker.Busy;

        // 7. The pure parts, as GateRunner exposes them.
        // Quoting round trip: a Python child prints back the arguments it received.
        var awkward = new List<string>
            { "plain", "with space", "trailing\\", "quote\"inside", "C:\\Program Files\\x\\", "", "héros" };
        var echo = new List<string> { "-c", "import sys, json; print(json.dumps(sys.argv[1:]))" };
        echo.AddRange(awkward);
        string echoed, echoErrors, echoFailure;
        GateRunner.RunProcess(GateRunner.StartInfo(new GateSettings { Python = python }, echo), 30, null,
                              out echoed, out echoErrors, out echoFailure);
        Result["quoted_sent"] = awkward;
        Result["quoted_received"] = echoed.Trim();
        var environment = new Dictionary<string, string>
        {
            { "PYTHONHOME", "x" }, { "PYTHONSTARTUP", "y" }, { "KEEP", "1" },
            { "PYTHONPATH", string.Join(Path.PathSeparator.ToString(), "/stub/Unity/Editor/Data/Python/lib", "/studio/site") },
        };
        GateRunner.CleanEnvironment(environment, new List<string> { "/stub/Unity/Editor/Data" });
        Result["clean_environment"] = environment;
        var settings = new GateSettings { Python = python, GateHome = gateHome, ProjectRoot = project };
        Result["arguments"] = GateRunner.BuildArguments(settings, Path.Combine(project, "Assets/Models/good.fbx"));
        Result["version"] = GateRunner.Version(settings);

        // 8. Batches: how files are split into runs, and how verdicts find their file.
        var many = Enumerable.Range(0, 120).Select(i => Path.Combine(project, "Loose", "SM_" + i + ".fbx")).ToList();
        Result["plan_counts"] = GateRunner.PlanBatches(settings, many).Select(run => run.Value.Count).ToList();
        string deep = Path.Combine(project, "Loose", new string('d', 380));
        var long_ = Enumerable.Range(0, 60).Select(i => Path.Combine(deep, "SM_" + i + ".fbx")).ToList();
        var longRuns = GateRunner.PlanBatches(settings, long_);
        Result["plan_long_counts"] = longRuns.Select(run => run.Value.Count).ToList();
        Result["plan_long_chars"] = longRuns.Select(run => GateRunner.Quote(GateRunner.ResolvePython(settings)).Length + 1
            + GateRunner.JoinArguments(GateRunner.BuildBatchArguments(settings,
                run.Value.Select(i => long_[i]).ToList(), run.Key)).Length).ToList();
        var mixed = new List<string>
        {
            Path.Combine(project, "Assets/Models/good.fbx"), Path.Combine(project, "Assets/My Textures/brick_Albedo.png"),
            Path.Combine(project, "Assets/Models/heavy.fbx"),
        };
        Result["plan_mixed"] = GateRunner.PlanBatches(settings, mixed)
            .Select(run => new Dictionary<string, object> { { "rules", run.Key }, { "files", run.Value } }).ToList();

        var canned = new List<GateResult>
        {
            new GateResult { FullPath = "C:\\Proj\\Assets\\A.fbx" }, new GateResult { FullPath = "C:\\Proj\\Assets\\b.png" },
            new GateResult { FullPath = "C:\\Proj\\Assets\\gone.fbx" }, new GateResult { FullPath = "C:\\Proj\\Assets\\none.obj" },
        };
        string cannedOut =
            "{\"file\": \"c:/proj/assets/B.PNG\", \"name\": \"B.PNG\", \"passed\": false, \"status\": \"FAILED\", \"metric\": \"\", \"problems\": [\"too big\"], \"warnings\": []}\n"
            + "{\"file\": \"C:\\\\PROJ\\\\Assets\\\\a.fbx\", \"name\": \"a.fbx\", \"passed\": true, \"status\": \"PASSED\", \"metric\": \"12 tris\", \"problems\": [], \"warnings\": []}\n"
            + "{\"file\": \"C:\\\\Proj\\\\Assets\\\\gone.fbx\", \"name\": \"gone.fbx\", \"passed\": false, \"status\": \"ERROR\", \"metric\": \"\", \"problems\": [\"not found\"], \"warnings\": []}\n"
            + "{\"summary\": {\"total\": 3, \"passed\": 1, \"failed\": 2, \"totals_failed\": []}}\n";
        GateRunner.ParseBatch(canned, cannedOut, "12:00:00 rules   C:\\Proj\\rules.toml\n", 1, true);
        Result["parse_windows"] = canned.Select(Describe).ToList();

        // The real gate on a batch holding a file that is gone (CheckBatch would not send it).
        // (heavy.fbx was fixed in step 3; the odd texture still fails.)
        var direct = new List<GateResult>
        {
            new GateResult { FullPath = Path.Combine(project, "Assets/Models/good.fbx") },
            new GateResult { FullPath = Path.Combine(project, "Assets/My Textures/héros_odd.png") },
            new GateResult { FullPath = Path.Combine(project, "Assets/Models/gone.fbx") },
            new GateResult { FullPath = Path.Combine(project, "Assets/My Textures/brick_Albedo.png") },
        };
        GateRunner.RunBatch(settings, direct, Path.Combine(project, "rules.toml"), null);
        Result["direct_batch"] = direct.Select(Describe).ToList();

        // A batch's timeout is the per-file timeout times its files.
        var slowSettings = new GateSettings { Python = python, GateHome = slowGateHome, ProjectRoot = project, TimeoutSeconds = 1 };
        var slowBatch = direct.Take(3).Select(r => new GateResult { FullPath = r.FullPath }).ToList();
        var slowClock = Stopwatch.StartNew();
        GateRunner.RunBatch(slowSettings, slowBatch, "", null);
        Result["batch_timeout_seconds"] = slowClock.Elapsed.TotalSeconds;
        Result["batch_timeout_reasons"] = slowBatch.Select(r => r.Reason).ToList();

        Console.WriteLine("AGK_HARNESS_RESULT " + JsonSerializer.Serialize(Result));
        return 0;
    }
}
