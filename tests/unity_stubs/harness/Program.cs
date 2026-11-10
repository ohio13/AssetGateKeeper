// Drives plugins/unity end to end without Unity: the real plug-in code, the real
// gate, and the stand-in UnityEditor from ../UnityStubs.cs. Run by
// tests/test_engine_plugins.py, which builds the project folder and checks the
// result printed on the last line (AGK_HARNESS_RESULT {...}).
//
//     dotnet run --project tests/unity_stubs/harness -- <project> <python> <gate folder> <slow gate folder>

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

    static int Main(string[] args)
    {
        string project = Path.GetFullPath(args[0]);
        string python = args[1];
        string gateHome = args[2];
        string slowGateHome = args[3];

        Application.dataPath = Path.Combine(project, "Assets");
        EditorPrefs.SetString("AssetGatekeeper.Python", python);
        EditorPrefs.SetString("AssetGatekeeper.GateHome", gateHome);
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

        // 2. The same import again (Unity re-imports when a label rewrites the .meta).
        int setLabels = AssetDatabase.SetLabelsCalls;
        Import(imported);
        Result["reimport_busy"] = AgkChecker.Busy;
        Pump(60);
        Result["reimport_lines"] = TakeLines();
        Result["reimport_set_labels"] = AssetDatabase.SetLabelsCalls - setLabels;

        // 3. The heavy model fixed on disk, then Check Selected on the Models folder.
        File.Copy(Path.Combine(project, "Assets/Models/good.fbx"), Path.Combine(project, "Assets/Models/heavy.fbx"), true);
        Selection.assetGUIDs = new[] { "Assets/Models" };
        Menu("CheckSelected");
        Pump(300);
        Result["fixed_lines"] = TakeLines();
        Result["fixed_labels"] = Labels();

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

        Console.WriteLine("AGK_HARNESS_RESULT " + JsonSerializer.Serialize(Result));
        return 0;
    }
}
