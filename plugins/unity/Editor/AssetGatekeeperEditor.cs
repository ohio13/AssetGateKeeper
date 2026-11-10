// AssetGatekeeper for Unity (2021.3 LTS or newer) - the editor side.
//
// * Checks models and textures when Unity imports them (an AssetPostprocessor,
//   batched through OnPostprocessAllAssets), on a background thread, one file at a
//   time, so the editor is never blocked.
// * Every failure goes to the Console as an error naming the asset and the gate's
//   own words (click it to ping the asset), and the asset gets the label
//   AGK_Failed: type  l:AGK_Failed  in the Project window's search to list them.
// * Tools > AssetGatekeeper > Check Selected / Check All / Select Failed Assets /
//   Settings (Preferences > AssetGatekeeper).
//
// The checking itself is GateRunner.cs: the same command line as the CLI and CI.

using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Threading;
using UnityEditor;
using UnityEngine;

namespace AssetGatekeeper.Editor
{
    /// <summary>Per-user settings (EditorPrefs): paths differ from machine to machine.</summary>
    public static class AgkSettings
    {
        const string Prefix = "AssetGatekeeper.";

        public static bool Enabled
        {
            get { return EditorPrefs.GetBool(Prefix + "Enabled", true); }
            set { EditorPrefs.SetBool(Prefix + "Enabled", value); }
        }

        /// <summary>Folder with asset_gatekeeper.py; empty = ASSETGATEKEEPER_HOME, else the pip install.</summary>
        public static string GateHome
        {
            get { return EditorPrefs.GetString(Prefix + "GateHome", ""); }
            set { EditorPrefs.SetString(Prefix + "GateHome", value ?? ""); }
        }

        /// <summary>Python with trimesh + Pillow; empty = ASSETGATEKEEPER_PYTHON, else python.</summary>
        public static string Python
        {
            get { return EditorPrefs.GetString(Prefix + "Python", ""); }
            set { EditorPrefs.SetString(Prefix + "Python", value ?? ""); }
        }

        /// <summary>One rules.toml for everything; empty = ASSETGATEKEEPER_RULES, else the nearest rules.toml.</summary>
        public static string RulesFile
        {
            get { return EditorPrefs.GetString(Prefix + "RulesFile", ""); }
            set { EditorPrefs.SetString(Prefix + "RulesFile", value ?? ""); }
        }

        public static int TimeoutSeconds
        {
            get { return Math.Max(5, EditorPrefs.GetInt(Prefix + "TimeoutSeconds", 120)); }
            set { EditorPrefs.SetInt(Prefix + "TimeoutSeconds", Math.Max(5, value)); }
        }

        /// <summary>Also write a Console line for every asset that passes.</summary>
        public static bool LogPasses
        {
            get { return EditorPrefs.GetBool(Prefix + "LogPasses", false); }
            set { EditorPrefs.SetBool(Prefix + "LogPasses", value); }
        }

        /// <summary>The Unity project folder (the one holding Assets/).</summary>
        public static string ProjectFolder
        {
            get { return Path.GetDirectoryName(Application.dataPath); }
        }

        /// <summary>A copy for the background thread (EditorPrefs is main-thread only).</summary>
        public static GateSettings Snapshot()
        {
            var settings = new GateSettings
            {
                Python = Python,
                GateHome = GateHome,
                RulesFile = RulesFile,
                TimeoutSeconds = TimeoutSeconds,
                ProjectRoot = ProjectFolder,
            };
            settings.HostRoots.Add(EditorApplication.applicationContentsPath);
            return settings;
        }
    }

    /// <summary>The queue, the worker thread and the reporting.</summary>
    [InitializeOnLoad]
    public static class AgkChecker
    {
        public const string FailedLabel = "AGK_Failed";
        const string Tag = "AssetGatekeeper: ";

        sealed class Item
        {
            public string AssetPath;
            public string FullPath;
            public GateSettings Settings;
        }

        static readonly object Gate = new object();
        static readonly Queue<Item> Pending = new Queue<Item>();
        static readonly HashSet<string> Queued = new HashSet<string>();
        static readonly ConcurrentQueue<GateResult> Finished = new ConcurrentQueue<GateResult>();
        static readonly ConcurrentQueue<Action> MainThread = new ConcurrentQueue<Action>();
        // assetPath -> size and time of the file as last checked. An import that does
        // not change the source (a label change rewrites the .meta) is not re-checked.
        static readonly Dictionary<string, string> Checked = new Dictionary<string, string>();
        static readonly HashSet<string> ReasonsShown = new HashSet<string>();
        static Thread worker;
        static volatile bool stopping;

        static int progressId = -1;
        static int batchTotal, batchDone, batchFailed, batchNotChecked;
        static bool batchManual;

        static AgkChecker()
        {
            EditorApplication.update += Drain;
            AssemblyReloadEvents.beforeAssemblyReload += Shutdown;
            EditorApplication.quitting += Shutdown;
        }

        public static bool IsCandidate(string assetPath)
        {
            return !string.IsNullOrEmpty(assetPath)
                   && assetPath.Replace('\\', '/').StartsWith("Assets/", StringComparison.Ordinal)
                   && GateRunner.IsGated(assetPath);
        }

        public static bool Busy
        {
            get
            {
                lock (Gate) return worker != null || Pending.Count > 0;
            }
        }

        static string Stamp(string fullPath)
        {
            try
            {
                var info = new FileInfo(fullPath);
                return info.Exists ? info.Length + ":" + info.LastWriteTimeUtc.Ticks : "";
            }
            catch (Exception)
            {
                return "";
            }
        }

        /// <summary>Queue asset paths (Assets/...) for checking. force: check even if unchanged.</summary>
        public static int Enqueue(IEnumerable<string> assetPaths, bool force, bool manual)
        {
            GateSettings settings = AgkSettings.Snapshot();
            string project = AgkSettings.ProjectFolder;
            int added = 0;
            lock (Gate)
            {
                foreach (string raw in assetPaths)
                {
                    string assetPath = raw.Replace('\\', '/');
                    if (!IsCandidate(assetPath) || Queued.Contains(assetPath)) continue;
                    string fullPath = Path.GetFullPath(Path.Combine(project, assetPath));
                    string stamp = Stamp(fullPath);
                    string previous;
                    if (!force && stamp.Length > 0 && Checked.TryGetValue(assetPath, out previous) && previous == stamp)
                        continue;
                    Checked[assetPath] = stamp;
                    Queued.Add(assetPath);
                    Pending.Enqueue(new Item { AssetPath = assetPath, FullPath = fullPath, Settings = settings });
                    added++;
                }
                if (added == 0) return 0;
                stopping = false;
                if (batchTotal == batchDone)
                {
                    batchTotal = batchDone = batchFailed = batchNotChecked = 0;
                    batchManual = false;
                    ReasonsShown.Clear();
                }
                batchTotal += added;
                batchManual |= manual;
                if (worker == null)
                {
                    worker = new Thread(Work) { IsBackground = true, Name = "AssetGatekeeper" };
                    worker.Start();
                }
            }
            if (progressId < 0 || !Progress.Exists(progressId))
            {
                progressId = Progress.Start("AssetGatekeeper", "Checking assets against the budget");
                Progress.RegisterCancelCallback(progressId, () =>
                {
                    Cancel();
                    return true;
                });
            }
            Progress.Report(progressId, batchDone, batchTotal, batchDone + " of " + batchTotal);
            return added;
        }

        static void Work()
        {
            while (true)
            {
                Item item;
                lock (Gate)
                {
                    if (Pending.Count == 0 || stopping)
                    {
                        worker = null;
                        return;
                    }
                    item = Pending.Dequeue();
                }
                GateResult result;
                try
                {
                    result = GateRunner.Check(item.Settings, item.FullPath, item.AssetPath, () => stopping);
                }
                catch (Exception error)
                {
                    result = new GateResult { AssetPath = item.AssetPath, FullPath = item.FullPath,
                                              Reason = "the plug-in failed: " + error.Message };
                }
                Finished.Enqueue(result);
            }
        }

        /// <summary>Drop what is queued and stop the file being checked.</summary>
        public static void Cancel()
        {
            lock (Gate)
            {
                foreach (Item item in Pending)
                {
                    Queued.Remove(item.AssetPath);
                    Checked.Remove(item.AssetPath);
                }
                int dropped = Pending.Count;
                Pending.Clear();
                batchTotal -= dropped;
                stopping = true;
            }
        }

        static void Shutdown()
        {
            Cancel();
            Thread running;
            lock (Gate) running = worker;
            if (running != null) running.Join(3000);
        }

        /// <summary>Run an action on the main thread at the next editor tick.</summary>
        public static void Post(Action action)
        {
            MainThread.Enqueue(action);
        }

        static void Drain()
        {
            Action action;
            while (MainThread.TryDequeue(out action))
            {
                try
                {
                    action();
                }
                catch (Exception error)
                {
                    Debug.LogException(error);
                }
            }
            GateResult result;
            bool any = false;
            while (Finished.TryDequeue(out result))
            {
                any = true;
                lock (Gate)
                {
                    Queued.Remove(result.AssetPath);
                    batchDone++;
                }
                if (result.Reason == "cancelled")
                {
                    lock (Gate) Checked.Remove(result.AssetPath);
                    continue;
                }
                Report(result);
            }
            if (!any) return;
            bool finished;
            lock (Gate) finished = batchDone >= batchTotal && Pending.Count == 0;
            if (progressId >= 0 && Progress.Exists(progressId))
            {
                if (finished)
                {
                    Progress.Finish(progressId, batchFailed > 0 ? Progress.Status.Failed : Progress.Status.Succeeded);
                    progressId = -1;
                }
                else
                {
                    Progress.Report(progressId, batchDone, batchTotal, batchDone + " of " + batchTotal);
                }
            }
            if (finished && (batchManual || batchFailed > 0 || batchNotChecked > 0))
            {
                string summary = Tag + batchDone + " asset(s) checked: " + (batchDone - batchFailed - batchNotChecked)
                                 + " passed, " + batchFailed + " failed"
                                 + (batchNotChecked > 0 ? ", " + batchNotChecked + " NOT checked" : "");
                if (batchFailed > 0) summary += "  (search l:" + FailedLabel + " in the Project window)";
                Debug.Log(summary);
            }
        }

        static void Report(GateResult result)
        {
            UnityEngine.Object asset = AssetDatabase.LoadMainAssetAtPath(result.AssetPath);
            switch (result.Outcome)
            {
                case GateOutcome.Passed:
                    SetFailedLabel(asset, false);
                    foreach (string warning in result.Warnings)
                        Debug.LogWarning(Tag + result.AssetPath + " note: " + warning, asset);
                    if (AgkSettings.LogPasses)
                        Debug.Log(Tag + result.AssetPath + " " + result.Status
                                  + (result.Metric.Length > 0 ? "  " + result.Metric : ""), asset);
                    break;
                case GateOutcome.Failed:
                    batchFailed++;
                    SetFailedLabel(asset, true);
                    Debug.LogError(FailureMessage(result), asset);
                    break;
                default:
                    batchNotChecked++;
                    // One line per distinct reason and batch: a missing Python would
                    // otherwise print the same warning for every file in the project.
                    if (ReasonsShown.Add(result.Reason))
                        Debug.LogWarning(Tag + result.AssetPath + " NOT checked: " + result.Reason
                                         + "  (Tools > AssetGatekeeper > Settings)", asset);
                    break;
            }
        }

        /// <summary>The Console line for a failed asset: path, status, then each of the gate's lines.</summary>
        public static string FailureMessage(GateResult result)
        {
            var message = Tag + result.AssetPath + " " + result.Status + ": " + string.Join("; ", result.Problems.ToArray());
            if (result.Problems.Count > 1)
            {
                foreach (string problem in result.Problems) message += "\n  - " + problem;
            }
            foreach (string warning in result.Warnings) message += "\n  note: " + warning;
            return message;
        }

        static void SetFailedLabel(UnityEngine.Object asset, bool failed)
        {
            if (asset == null) return;
            var labels = new List<string>(AssetDatabase.GetLabels(asset));
            bool has = labels.Contains(FailedLabel);
            if (has == failed) return;          // unchanged: leave the .meta alone
            if (failed) labels.Add(FailedLabel);
            else labels.Remove(FailedLabel);
            AssetDatabase.SetLabels(asset, labels.ToArray());
        }

        /// <summary>Every gated asset under the selection (files, and the contents of selected folders).</summary>
        public static List<string> SelectedAssetPaths()
        {
            var paths = new List<string>();
            foreach (string guid in Selection.assetGUIDs)
            {
                string path = AssetDatabase.GUIDToAssetPath(guid);
                if (AssetDatabase.IsValidFolder(path))
                {
                    foreach (string inner in AssetDatabase.FindAssets("", new[] { path }))
                        paths.Add(AssetDatabase.GUIDToAssetPath(inner));
                }
                else
                {
                    paths.Add(path);
                }
            }
            return paths;
        }

        public static List<string> AllAssetPaths()
        {
            var paths = new List<string>();
            foreach (string path in AssetDatabase.GetAllAssetPaths())
            {
                if (IsCandidate(path)) paths.Add(path);
            }
            return paths;
        }
    }

    /// <summary>Checks what Unity has just imported or moved.</summary>
    public class AgkPostprocessor : AssetPostprocessor
    {
        static void OnPostprocessAllAssets(string[] importedAssets, string[] deletedAssets,
                                           string[] movedAssets, string[] movedFromAssetPaths)
        {
            if (!AgkSettings.Enabled) return;
            var paths = new List<string>();
            foreach (string path in importedAssets)
                if (AgkChecker.IsCandidate(path)) paths.Add(path);
            // A moved file may now fall under a different rules.toml.
            foreach (string path in movedAssets)
                if (AgkChecker.IsCandidate(path)) paths.Add(path);
            if (paths.Count > 0) AgkChecker.Enqueue(paths, false, false);
        }
    }

    public static class AgkMenu
    {
        const string Root = "Tools/AssetGatekeeper/";

        [MenuItem(Root + "Check Selected", false, 1)]
        static void CheckSelected()
        {
            var paths = AgkChecker.SelectedAssetPaths();
            int added = AgkChecker.Enqueue(paths, true, true);
            if (added == 0)
                Debug.Log("AssetGatekeeper: nothing to check in the selection (models: .fbx .obj .gltf .glb; "
                          + "textures: .png .tga .jpg .tif .webp .dds .exr .ktx2)");
        }

        [MenuItem(Root + "Check Selected", true)]
        static bool CheckSelectedValid()
        {
            return Selection.assetGUIDs != null && Selection.assetGUIDs.Length > 0;
        }

        [MenuItem(Root + "Check All", false, 2)]
        static void CheckAll()
        {
            int added = AgkChecker.Enqueue(AgkChecker.AllAssetPaths(), true, true);
            Debug.Log("AssetGatekeeper: checking " + added + " asset(s) in the background");
        }

        [MenuItem(Root + "Select Failed Assets", false, 20)]
        static void SelectFailed()
        {
            var found = new List<UnityEngine.Object>();
            foreach (string guid in AssetDatabase.FindAssets("l:" + AgkChecker.FailedLabel))
            {
                UnityEngine.Object asset = AssetDatabase.LoadMainAssetAtPath(AssetDatabase.GUIDToAssetPath(guid));
                if (asset != null) found.Add(asset);
            }
            Selection.objects = found.ToArray();
            Debug.Log("AssetGatekeeper: " + found.Count + " asset(s) labelled " + AgkChecker.FailedLabel);
        }

        [MenuItem(Root + "Stop Checking", false, 21)]
        static void Stop()
        {
            AgkChecker.Cancel();
        }

        [MenuItem(Root + "Stop Checking", true)]
        static bool StopValid()
        {
            return AgkChecker.Busy;
        }

        [MenuItem(Root + "Settings...", false, 40)]
        static void OpenSettings()
        {
            SettingsService.OpenUserPreferences(AgkSettingsProvider.SettingsPath);
        }
    }

    /// <summary>Edit > Preferences > AssetGatekeeper (macOS: Unity > Settings).</summary>
    public static class AgkSettingsProvider
    {
        public const string SettingsPath = "Preferences/AssetGatekeeper";
        static string testReply = "";

        [SettingsProvider]
        public static SettingsProvider Create()
        {
            var provider = new SettingsProvider(SettingsPath, SettingsScope.User)
            {
                label = "AssetGatekeeper",
                guiHandler = searchContext => Draw(),
                keywords = new HashSet<string>(new[] { "AssetGatekeeper", "budget", "gate", "python", "rules" }),
            };
            return provider;
        }

        static void Draw()
        {
            EditorGUILayout.HelpBox(
                "Leave a path empty to use the environment variable named after it, as the other "
                + "AssetGatekeeper plug-ins do. Without a gate folder, the pip install is used "
                + "(python -m asset_gatekeeper).", MessageType.None);

            AgkSettings.Enabled = EditorGUILayout.Toggle("Check on import", AgkSettings.Enabled);
            AgkSettings.GateHome = PathField("Gate folder", AgkSettings.GateHome, GateRunner.EnvHome, true);
            AgkSettings.Python = PathField("Python executable", AgkSettings.Python, GateRunner.EnvPython, false);
            AgkSettings.RulesFile = PathField("Rules file (optional)", AgkSettings.RulesFile, GateRunner.EnvRules, false);
            AgkSettings.TimeoutSeconds = EditorGUILayout.IntField("Timeout per file (s)", AgkSettings.TimeoutSeconds);
            AgkSettings.LogPasses = EditorGUILayout.Toggle("Log passing assets", AgkSettings.LogPasses);

            EditorGUILayout.Space();
            GateSettings snapshot = AgkSettings.Snapshot();
            string script = GateRunner.ResolveGateScript(snapshot);
            EditorGUILayout.LabelField("Runs", GateRunner.ResolvePython(snapshot) + " "
                                       + (script.Length > 0 ? script : "-m asset_gatekeeper") + " <file> --once --json");
            EditorGUILayout.LabelField("Rules", "the setting, " + GateRunner.EnvRules
                                       + ", or the nearest rules.toml up to the project folder");
            if (GUILayout.Button("Test the gate", GUILayout.Width(140)))
            {
                testReply = "running...";
                var thread = new Thread(() =>
                {
                    string reply = GateRunner.Version(snapshot);
                    AgkChecker.Post(() =>
                    {
                        testReply = reply;
                        Debug.Log("AssetGatekeeper: test: " + reply);
                    });
                }) { IsBackground = true };
                thread.Start();
            }
            if (testReply.Length > 0) EditorGUILayout.HelpBox(testReply, MessageType.Info);
        }

        static string PathField(string label, string value, string environmentName, bool folder)
        {
            EditorGUILayout.BeginHorizontal();
            string edited = EditorGUILayout.TextField(new GUIContent(label, "Empty: " + environmentName), value);
            if (GUILayout.Button("Browse", GUILayout.Width(70)))
            {
                string picked = folder
                    ? EditorUtility.OpenFolderPanel(label, value, "")
                    : EditorUtility.OpenFilePanel(label, string.IsNullOrEmpty(value) ? "" : Path.GetDirectoryName(value), "");
                if (!string.IsNullOrEmpty(picked)) edited = picked;
                GUI.FocusControl(null);
            }
            EditorGUILayout.EndHorizontal();
            return edited;
        }
    }
}
