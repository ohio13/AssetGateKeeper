// Stand-ins for the parts of UnityEngine / UnityEditor that plugins/unity uses.
//
// Test-only. They let the plug-in compile without Unity (as netstandard2.1, the
// API level Unity 2021.3 uses by default, with C# 9) and let tests/unity_stubs/harness
// drive it end to end: the stand-ins record Console lines and asset labels instead
// of showing them. The signatures follow Unity 2021.3's scripting reference; only
// what the plug-in calls is here, so a call to anything else fails to compile.

using System;
using System.Collections.Generic;

namespace UnityEngine
{
    public class Object
    {
        public string name = "";
    }

    public struct Rect
    {
    }

    public sealed class GUILayoutOption
    {
    }

    public class GUIContent
    {
        public string text;
        public string tooltip;

        public GUIContent(string text, string tooltip)
        {
            this.text = text;
            this.tooltip = tooltip;
        }
    }

    public static class GUI
    {
        public static void FocusControl(string name)
        {
        }
    }

    public static class GUILayout
    {
        public static bool Button(string text, params GUILayoutOption[] options)
        {
            return false;
        }

        public static GUILayoutOption Width(float width)
        {
            return new GUILayoutOption();
        }
    }

    public static class Application
    {
        /// <summary>Stand-in: the harness points this at its project's Assets folder.</summary>
        public static string dataPath = "";
    }

    public static class Debug
    {
        /// <summary>Stand-in: every Console line, as "error|warning|log: text".</summary>
        public static readonly List<string> Lines = new List<string>();
        public static readonly List<Object> Contexts = new List<Object>();

        static void Add(string kind, object message, Object context)
        {
            lock (Lines)
            {
                Lines.Add(kind + ": " + message);
                Contexts.Add(context);
            }
            Console.WriteLine("[Unity " + kind + "] " + message);
        }

        public static void Log(object message) { Add("log", message, null); }
        public static void Log(object message, Object context) { Add("log", message, context); }
        public static void LogWarning(object message) { Add("warning", message, null); }
        public static void LogWarning(object message, Object context) { Add("warning", message, context); }
        public static void LogError(object message) { Add("error", message, null); }
        public static void LogError(object message, Object context) { Add("error", message, context); }
        public static void LogException(Exception exception) { Add("exception", exception, null); }
    }
}

namespace UnityEditor
{
    using UnityEngine;

    [AttributeUsage(AttributeTargets.Class)]
    public sealed class InitializeOnLoadAttribute : Attribute
    {
    }

    [AttributeUsage(AttributeTargets.Method, AllowMultiple = true)]
    public sealed class MenuItem : Attribute
    {
        public string menuItem;
        public bool validate;
        public int priority;

        public MenuItem(string itemName) : this(itemName, false) { }
        public MenuItem(string itemName, bool isValidateFunction) : this(itemName, isValidateFunction, 1000) { }

        public MenuItem(string itemName, bool isValidateFunction, int priority)
        {
            menuItem = itemName;
            validate = isValidateFunction;
            this.priority = priority;
        }
    }

    [AttributeUsage(AttributeTargets.Method)]
    public sealed class SettingsProviderAttribute : Attribute
    {
    }

    public enum SettingsScope
    {
        User,
        Project,
    }

    public class SettingsProvider
    {
        public string settingsPath;
        public SettingsScope scope;
        public string label { get; set; }
        public Action<string> guiHandler { get; set; }
        public IEnumerable<string> keywords { get; set; }

        public SettingsProvider(string path, SettingsScope scopes, IEnumerable<string> keywords = null)
        {
            settingsPath = path;
            scope = scopes;
            this.keywords = keywords;
        }
    }

    public static class SettingsService
    {
        public static void OpenUserPreferences(string settingsPath = null)
        {
        }
    }

    public enum MessageType
    {
        None,
        Info,
        Warning,
        Error,
    }

    public static class EditorGUILayout
    {
        public static void HelpBox(string message, MessageType type) { }
        public static bool Toggle(string label, bool value, params GUILayoutOption[] options) { return value; }
        public static string TextField(GUIContent label, string text, params GUILayoutOption[] options) { return text; }
        public static int IntField(string label, int value, params GUILayoutOption[] options) { return value; }
        public static void Space() { }
        public static void LabelField(string label, string label2, params GUILayoutOption[] options) { }
        public static Rect BeginHorizontal(params GUILayoutOption[] options) { return new Rect(); }
        public static void EndHorizontal() { }
    }

    public static class EditorUtility
    {
        public static string OpenFolderPanel(string title, string folder, string defaultName) { return ""; }
        public static string OpenFilePanel(string title, string directory, string extension) { return ""; }
    }

    public static class EditorPrefs
    {
        /// <summary>Stand-in storage.</summary>
        public static readonly Dictionary<string, object> Values = new Dictionary<string, object>();

        static T Get<T>(string key, T fallback)
        {
            object value;
            return Values.TryGetValue(key, out value) && value is T ? (T)value : fallback;
        }

        public static bool GetBool(string key, bool defaultValue = false) { return Get(key, defaultValue); }
        public static void SetBool(string key, bool value) { Values[key] = value; }
        public static string GetString(string key, string defaultValue = "") { return Get(key, defaultValue); }
        public static void SetString(string key, string value) { Values[key] = value; }
        public static int GetInt(string key, int defaultValue = 0) { return Get(key, defaultValue); }
        public static void SetInt(string key, int value) { Values[key] = value; }
    }

    public static class EditorApplication
    {
        public delegate void CallbackFunction();

        public static CallbackFunction update;
        public static event Action quitting;
        public static string applicationContentsPath = "/stub/Unity/Editor/Data";

        /// <summary>Stand-in: one editor tick.</summary>
        public static void Tick()
        {
            if (update != null) update();
        }

        /// <summary>Stand-in: the editor closing.</summary>
        public static void Quit()
        {
            if (quitting != null) quitting();
        }
    }

    public static class AssemblyReloadEvents
    {
        public delegate void AssemblyReloadCallback();

        public static event AssemblyReloadCallback beforeAssemblyReload;

        /// <summary>Stand-in: a script recompile.</summary>
        public static void Reload()
        {
            if (beforeAssemblyReload != null) beforeAssemblyReload();
        }
    }

    public static class Progress
    {
        public enum Status
        {
            Running,
            Succeeded,
            Failed,
            Canceled,
            Paused,
        }

        [Flags]
        public enum Options
        {
            None = 0,
        }

        static readonly Dictionary<int, Func<bool>> Running = new Dictionary<int, Func<bool>>();
        static int next = 1;
        /// <summary>Stand-in: the last finish status, for the harness.</summary>
        public static Status LastFinish = Status.Running;

        public static int Start(string name, string description = null, Options options = Options.None, int parentId = -1)
        {
            int id = next++;
            Running[id] = null;
            return id;
        }

        public static bool Exists(int id) { return Running.ContainsKey(id); }
        public static void Report(int id, int currentStep, int totalSteps, string description = null) { }

        public static void Finish(int id, Status status = Status.Succeeded)
        {
            Running.Remove(id);
            LastFinish = status;
        }

        public static void RegisterCancelCallback(int id, Func<bool> callback)
        {
            Running[id] = callback;
        }
    }

    public class AssetPostprocessor
    {
    }

    public static class Selection
    {
        public static string[] assetGUIDs = new string[0];
        public static Object[] objects { get; set; }
    }

    /// <summary>
    /// Stand-in AssetDatabase over a folder: an asset's GUID is its path, every file is
    /// an asset, and labels are kept in memory.
    /// </summary>
    public static class AssetDatabase
    {
        public static readonly Dictionary<string, string[]> Labels = new Dictionary<string, string[]>();
        public static int SetLabelsCalls;
        static readonly Dictionary<string, Object> Loaded = new Dictionary<string, Object>();

        static string Project
        {
            get { return System.IO.Path.GetDirectoryName(Application.dataPath); }
        }

        static string Full(string assetPath)
        {
            return System.IO.Path.Combine(Project, assetPath);
        }

        public static Object LoadMainAssetAtPath(string assetPath)
        {
            if (!System.IO.File.Exists(Full(assetPath))) return null;
            Object asset;
            if (!Loaded.TryGetValue(assetPath, out asset))
            {
                asset = new Object { name = assetPath };
                Loaded[assetPath] = asset;
            }
            return asset;
        }

        public static string[] GetLabels(Object obj)
        {
            string[] labels;
            return Labels.TryGetValue(obj.name, out labels) ? labels : new string[0];
        }

        public static void SetLabels(Object obj, string[] labels)
        {
            SetLabelsCalls++;
            Labels[obj.name] = labels;
        }

        public static string GUIDToAssetPath(string guid) { return guid; }
        public static bool IsValidFolder(string path) { return System.IO.Directory.Exists(Full(path)); }

        public static string[] FindAssets(string filter)
        {
            return FindAssets(filter, new[] { "Assets" });
        }

        public static string[] FindAssets(string filter, string[] searchInFolders)
        {
            var found = new List<string>();
            foreach (string folder in searchInFolders)
            {
                foreach (string file in System.IO.Directory.GetFiles(Full(folder), "*", System.IO.SearchOption.AllDirectories))
                {
                    string path = System.IO.Path.GetRelativePath(Project, file).Replace('\\', '/');
                    if (filter.StartsWith("l:"))
                    {
                        string[] labels;
                        if (!Labels.TryGetValue(path, out labels) || Array.IndexOf(labels, filter.Substring(2)) < 0) continue;
                    }
                    found.Add(path);
                }
            }
            return found.ToArray();
        }

        public static string[] GetAllAssetPaths()
        {
            return FindAssets("");
        }
    }
}
