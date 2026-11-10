// AssetGatekeeper for Unity - the part that runs the gate.
//
// Nothing in this file uses UnityEngine or UnityEditor, on purpose: it is the
// part that can be compiled and run outside Unity (tests/unity_stubs runs it
// against the real gate in CI), and it keeps the Unity-facing file small.
//
// The gate is the same command line the CLI, CI and the Unreal plug-in use:
//
//     <python> <gate folder>/asset_gatekeeper.py <file> --once --json [--rules <rules.toml>]
//
// or, when no gate folder is set, `<python> -m asset_gatekeeper ...` (the pip
// install). It prints one JSON object for the file on stdout; its log lines go
// to stderr. Exit codes: 0 passed, 1 failed, 2 bad setup.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text;

namespace AssetGatekeeper.Editor
{
    /// <summary>What the gate said about one file.</summary>
    public enum GateOutcome
    {
        /// <summary>Within budget.</summary>
        Passed,
        /// <summary>Over budget, or the gate could not read the file (status FAILED or ERROR).</summary>
        Failed,
        /// <summary>The gate did not give a verdict: not installed, crashed, timed out.</summary>
        NotChecked,
    }

    /// <summary>The verdict for one file, as the plug-in uses it.</summary>
    public sealed class GateResult
    {
        public string AssetPath = "";
        public string FullPath = "";
        public GateOutcome Outcome = GateOutcome.NotChecked;
        /// <summary>The gate's status word: PASSED, FAILED, ERROR, HEALED... or "" when there was no verdict.</summary>
        public string Status = "";
        public string Metric = "";
        public List<string> Problems = new List<string>();
        public List<string> Warnings = new List<string>();
        /// <summary>Why there is no verdict (NotChecked only).</summary>
        public string Reason = "";
        public int ExitCode = -1;
    }

    /// <summary>Where the gate is and how to run it. Empty strings mean "not set".</summary>
    public sealed class GateSettings
    {
        /// <summary>Interpreter with trimesh and Pillow. Empty: ASSETGATEKEEPER_PYTHON, else python / python3.</summary>
        public string Python = "";
        /// <summary>Folder holding asset_gatekeeper.py. Empty: ASSETGATEKEEPER_HOME, else `python -m asset_gatekeeper`.</summary>
        public string GateHome = "";
        /// <summary>A rules.toml to use for every file. Empty: ASSETGATEKEEPER_RULES, else the nearest rules.toml.</summary>
        public string RulesFile = "";
        /// <summary>Seconds one file may take before the gate is stopped.</summary>
        public int TimeoutSeconds = 120;
        /// <summary>Highest folder the rules.toml search climbs to (the Unity project folder).</summary>
        public string ProjectRoot = "";
        /// <summary>Folders of the host application's own runtime; PYTHONPATH entries inside them are dropped.</summary>
        public List<string> HostRoots = new List<string>();
    }

    public static class GateRunner
    {
        public const string EnvPython = "ASSETGATEKEEPER_PYTHON";
        public const string EnvHome = "ASSETGATEKEEPER_HOME";
        public const string EnvRules = "ASSETGATEKEEPER_RULES";
        public const string RulesFileName = "rules.toml";

        /// <summary>Source formats the gate measures (its TRACKED_SUFFIXES). Anything else is left alone.</summary>
        public static readonly string[] GatedExtensions =
        {
            ".fbx", ".obj", ".gltf", ".glb",
            ".png", ".tga", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".dds", ".exr", ".ktx2",
        };

        /// <summary>
        /// Variables that point a child interpreter at some other Python install. Inherited
        /// by the gate's interpreter (a different version), they make it load the wrong
        /// standard library and die before measuring anything - the "SRE module mismatch"
        /// seen in Unreal when the editor was started from Houdini.
        /// </summary>
        public static readonly string[] InheritedPythonVariables =
        {
            "PYTHONHOME", "PYTHONEXECUTABLE", "PYTHONSTARTUP", "__PYVENV_LAUNCHER__",
        };

        public static bool IsGated(string path)
        {
            string extension = Path.GetExtension(path ?? "").ToLowerInvariant();
            return Array.IndexOf(GatedExtensions, extension) >= 0;
        }

        static string Env(string name)
        {
            return (Environment.GetEnvironmentVariable(name) ?? "").Trim();
        }

        public static bool IsWindows
        {
            get { return Path.DirectorySeparatorChar == '\\'; }
        }

        public static string ResolvePython(GateSettings settings)
        {
            if (!string.IsNullOrEmpty(settings.Python.Trim())) return settings.Python.Trim();
            string fromEnv = Env(EnvPython);
            if (fromEnv.Length > 0) return fromEnv;
            return IsWindows ? "python" : "python3";
        }

        /// <summary>The gate script, or "" for `python -m asset_gatekeeper`.</summary>
        public static string ResolveGateScript(GateSettings settings)
        {
            string home = settings.GateHome.Trim();
            if (home.Length == 0) home = Env(EnvHome);
            if (home.Length == 0) return "";
            if (home.EndsWith(".py", StringComparison.OrdinalIgnoreCase)) return home;
            return Path.Combine(home, "asset_gatekeeper.py");
        }

        /// <summary>
        /// The rules.toml for this file: the setting, ASSETGATEKEEPER_RULES, or the nearest
        /// rules.toml in the file's folder or a parent, up to the project folder. "" = the
        /// gate's built-in defaults.
        /// </summary>
        public static string ResolveRules(GateSettings settings, string fullPath)
        {
            string explicitRules = settings.RulesFile.Trim();
            if (explicitRules.Length == 0) explicitRules = Env(EnvRules);
            if (explicitRules.Length > 0) return explicitRules;

            string stop = string.IsNullOrEmpty(settings.ProjectRoot)
                ? "" : TrimSeparators(Path.GetFullPath(settings.ProjectRoot));
            string folder = Path.GetDirectoryName(Path.GetFullPath(fullPath));
            while (!string.IsNullOrEmpty(folder))
            {
                string candidate = Path.Combine(folder, RulesFileName);
                if (File.Exists(candidate)) return candidate;
                if (stop.Length == 0 || string.Equals(TrimSeparators(folder), stop, PathComparison)) break;
                string parent = Path.GetDirectoryName(folder);
                if (parent == null || parent == folder) break;
                folder = parent;
            }
            return "";
        }

        static StringComparison PathComparison
        {
            get { return IsWindows ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal; }
        }

        static string TrimSeparators(string path)
        {
            return path.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        }

        /// <summary>The argument list after the interpreter (unquoted).</summary>
        public static List<string> BuildArguments(GateSettings settings, string fullPath)
        {
            var arguments = new List<string>();
            string script = ResolveGateScript(settings);
            if (script.Length > 0)
            {
                arguments.Add(script);
            }
            else
            {
                arguments.Add("-m");
                arguments.Add("asset_gatekeeper");
            }
            arguments.Add(fullPath);
            arguments.Add("--once");
            arguments.Add("--json");
            string rules = ResolveRules(settings, fullPath);
            if (rules.Length > 0)
            {
                arguments.Add("--rules");
                arguments.Add(rules);
            }
            return arguments;
        }

        /// <summary>
        /// One command line from an argument list, quoted the way Windows (and .NET/Mono on
        /// every platform) splits ProcessStartInfo.Arguments back apart. netstandard2.1 has
        /// no ProcessStartInfo.ArgumentList, so this is done by hand.
        /// </summary>
        public static string JoinArguments(IList<string> arguments)
        {
            var line = new StringBuilder();
            foreach (string argument in arguments)
            {
                if (line.Length > 0) line.Append(' ');
                line.Append(Quote(argument));
            }
            return line.ToString();
        }

        public static string Quote(string argument)
        {
            if (argument.Length > 0 && argument.IndexOfAny(new[] { ' ', '\t', '"', '\n' }) < 0)
                return argument;
            var quoted = new StringBuilder("\"");
            int backslashes = 0;
            foreach (char c in argument)
            {
                if (c == '\\')
                {
                    backslashes++;
                    continue;
                }
                if (c == '"')
                {
                    quoted.Append('\\', backslashes * 2 + 1);
                }
                else
                {
                    quoted.Append('\\', backslashes);
                }
                backslashes = 0;
                quoted.Append(c);
            }
            quoted.Append('\\', backslashes * 2);
            quoted.Append('"');
            return quoted.ToString();
        }

        /// <summary>
        /// The child's environment: the editor's, without the variables that would point
        /// the gate's Python at another install, and without PYTHONPATH entries inside the
        /// host application's own folders. The studio's PYTHONPATH entries are kept.
        /// </summary>
        public static void CleanEnvironment(IDictionary<string, string> environment, IList<string> hostRoots)
        {
            foreach (string name in InheritedPythonVariables)
            {
                environment.Remove(name);
            }
            string pythonPath;
            if (!environment.TryGetValue("PYTHONPATH", out pythonPath) || string.IsNullOrEmpty(pythonPath))
            {
                environment.Remove("PYTHONPATH");
                return;
            }
            var kept = new List<string>();
            foreach (string entry in pythonPath.Split(Path.PathSeparator))
            {
                if (entry.Length == 0 || IsUnder(entry, hostRoots)) continue;
                kept.Add(entry);
            }
            if (kept.Count > 0)
                environment["PYTHONPATH"] = string.Join(Path.PathSeparator.ToString(), kept.ToArray());
            else
                environment.Remove("PYTHONPATH");
        }

        static bool IsUnder(string path, IList<string> roots)
        {
            if (roots == null) return false;
            string candidate;
            try
            {
                candidate = TrimSeparators(Path.GetFullPath(path));
            }
            catch (Exception)
            {
                return false;
            }
            foreach (string root in roots)
            {
                if (string.IsNullOrEmpty(root)) continue;
                string normalised;
                try
                {
                    normalised = TrimSeparators(Path.GetFullPath(root));
                }
                catch (Exception)
                {
                    continue;
                }
                if (string.Equals(candidate, normalised, PathComparison)) return true;
                if (candidate.StartsWith(normalised + Path.DirectorySeparatorChar, PathComparison)) return true;
            }
            return false;
        }

        /// <summary>Process start info for one file (not started).</summary>
        public static ProcessStartInfo StartInfo(GateSettings settings, IList<string> arguments)
        {
            var info = new ProcessStartInfo
            {
                FileName = ResolvePython(settings),
                Arguments = JoinArguments(arguments),
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
            };
            if (!string.IsNullOrEmpty(settings.ProjectRoot) && Directory.Exists(settings.ProjectRoot))
                info.WorkingDirectory = settings.ProjectRoot;
            // Non-ASCII file names: the gate writes UTF-8 when told to.
            // ProcessStartInfo.Environment starts as a copy of the editor's environment.
            info.Environment["PYTHONIOENCODING"] = "utf-8";
            CleanEnvironment(info.Environment, settings.HostRoots);
            return info;
        }

        /// <summary>Run a command with a timeout; the process is killed when it runs out.</summary>
        public static int RunProcess(ProcessStartInfo info, int timeoutSeconds, Func<bool> cancelled,
                                     out string stdout, out string stderr, out string failure)
        {
            var output = new StringBuilder();
            var errors = new StringBuilder();
            stdout = "";
            stderr = "";
            failure = "";
            using (var process = new Process { StartInfo = info })
            {
                process.OutputDataReceived += (sender, e) =>
                {
                    if (e.Data != null) lock (output) output.AppendLine(e.Data);
                };
                process.ErrorDataReceived += (sender, e) =>
                {
                    if (e.Data != null) lock (errors) errors.AppendLine(e.Data);
                };
                try
                {
                    process.Start();
                }
                catch (Exception error)
                {
                    failure = "could not start " + info.FileName + " (" + error.Message
                              + "); set the Python executable in the AssetGatekeeper settings";
                    return -1;
                }
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                var clock = Stopwatch.StartNew();
                long limit = Math.Max(1, timeoutSeconds) * 1000L;
                while (!process.WaitForExit(100))
                {
                    bool stop = cancelled != null && cancelled();
                    if (stop || clock.ElapsedMilliseconds > limit)
                    {
                        try
                        {
                            process.Kill();
                        }
                        catch (Exception)
                        {
                            // already gone
                        }
                        process.WaitForExit(2000);
                        failure = stop ? "cancelled" : "the gate took longer than " + timeoutSeconds
                                  + " s and was stopped (raise the timeout in the settings)";
                        return -1;
                    }
                }
                process.WaitForExit();   // flushes the asynchronous readers
                lock (output) stdout = output.ToString();
                lock (errors) stderr = errors.ToString();
                return process.ExitCode;
            }
        }

        /// <summary>Check one file. Never throws; a gate that cannot say is NotChecked with the reason.</summary>
        public static GateResult Check(GateSettings settings, string fullPath, string assetPath, Func<bool> cancelled = null)
        {
            var result = new GateResult { AssetPath = assetPath ?? fullPath, FullPath = fullPath };
            if (!File.Exists(fullPath))
            {
                result.Reason = "source file not found";
                return result;
            }
            string script = ResolveGateScript(settings);
            if (script.Length > 0 && !File.Exists(script))
            {
                result.Reason = "the gate is not at " + script + " (check the gate folder setting or "
                                + EnvHome + ")";
                return result;
            }
            ProcessStartInfo info;
            try
            {
                info = StartInfo(settings, BuildArguments(settings, fullPath));
            }
            catch (Exception error)
            {
                result.Reason = "could not prepare the gate: " + error.Message;
                return result;
            }
            string stdout, stderr, failure;
            int exitCode = RunProcess(info, settings.TimeoutSeconds, cancelled, out stdout, out stderr, out failure);
            if (failure.Length > 0)
            {
                result.Reason = failure;
                return result;
            }
            return Parse(result, stdout, stderr, exitCode);
        }

        /// <summary>
        /// Read the gate's output: the last JSON object on stdout that has "problems" is the
        /// verdict (the same rule as the Unreal plug-in). No such line is NotChecked, with
        /// the last line the gate wrote as the reason (e.g. "missing dependencies: trimesh").
        /// </summary>
        public static GateResult Parse(GateResult result, string stdout, string stderr, int exitCode)
        {
            result.ExitCode = exitCode;
            string[] lines = (stdout ?? "").Split('\n');
            for (int i = lines.Length - 1; i >= 0; i--)
            {
                string line = lines[i].Trim();
                if (!line.StartsWith("{")) continue;
                Dictionary<string, object> verdict;
                try
                {
                    verdict = MiniJson.Parse(line) as Dictionary<string, object>;
                }
                catch (FormatException)
                {
                    continue;
                }
                if (verdict == null || !verdict.ContainsKey("problems")) continue;
                bool passed = verdict.ContainsKey("passed") && verdict["passed"] is bool && (bool)verdict["passed"];
                result.Outcome = passed ? GateOutcome.Passed : GateOutcome.Failed;
                result.Status = AsString(verdict, "status");
                if (result.Status.Length == 0) result.Status = passed ? "PASSED" : "FAILED";
                result.Metric = AsString(verdict, "metric");
                result.Problems = AsStrings(verdict, "problems");
                result.Warnings = AsStrings(verdict, "warnings");
                if (!passed && result.Problems.Count == 0)
                    result.Problems.Add("failed (exit " + exitCode + ") without a reason");
                return result;
            }
            string detail = LastLine(stderr);
            if (detail.Length == 0) detail = LastLine(stdout);
            result.Outcome = GateOutcome.NotChecked;
            result.Reason = "the gate gave no verdict (exit " + exitCode + ")"
                            + (detail.Length > 0 ? ": " + Clip(detail, 300) : "");
            return result;
        }

        static string LastLine(string text)
        {
            string[] lines = (text ?? "").Split('\n');
            for (int i = lines.Length - 1; i >= 0; i--)
            {
                string line = lines[i].Trim();
                if (line.Length > 0) return line;
            }
            return "";
        }

        static string Clip(string text, int length)
        {
            return text.Length <= length ? text : text.Substring(0, length) + "...";
        }

        static string AsString(Dictionary<string, object> map, string key)
        {
            object value;
            if (!map.TryGetValue(key, out value) || value == null) return "";
            return value as string ?? Convert.ToString(value, CultureInfo.InvariantCulture);
        }

        static List<string> AsStrings(Dictionary<string, object> map, string key)
        {
            var list = new List<string>();
            object value;
            if (!map.TryGetValue(key, out value)) return list;
            var items = value as List<object>;
            if (items == null) return list;
            foreach (object item in items)
            {
                if (item != null) list.Add(item as string ?? Convert.ToString(item, CultureInfo.InvariantCulture));
            }
            return list;
        }

        /// <summary>`--version`, for the settings page's Test button. Returns the reply or the reason it failed.</summary>
        public static string Version(GateSettings settings)
        {
            var arguments = new List<string>();
            string script = ResolveGateScript(settings);
            if (script.Length > 0)
            {
                if (!File.Exists(script)) return "the gate is not at " + script;
                arguments.Add(script);
            }
            else
            {
                arguments.Add("-m");
                arguments.Add("asset_gatekeeper");
            }
            arguments.Add("--version");
            string stdout, stderr, failure;
            int code = RunProcess(StartInfo(settings, arguments), 30, null, out stdout, out stderr, out failure);
            if (failure.Length > 0) return failure;
            string reply = LastLine(stdout);
            if (code == 0 && reply.Length > 0) return reply;
            string detail = LastLine(stderr);
            return "exit " + code + (detail.Length > 0 ? ": " + Clip(detail, 300) : "");
        }
    }

    /// <summary>
    /// A small JSON reader (objects, arrays, strings, numbers, true/false/null), so the
    /// plug-in needs no package: JsonUtility cannot read a free-form object such as "stats".
    /// </summary>
    public static class MiniJson
    {
        public static object Parse(string text)
        {
            int index = 0;
            object value = ReadValue(text, ref index);
            SkipSpace(text, ref index);
            if (index != text.Length) throw new FormatException("trailing text at " + index);
            return value;
        }

        static void SkipSpace(string text, ref int index)
        {
            while (index < text.Length && char.IsWhiteSpace(text[index])) index++;
        }

        static object ReadValue(string text, ref int index)
        {
            SkipSpace(text, ref index);
            if (index >= text.Length) throw new FormatException("unexpected end");
            char c = text[index];
            if (c == '{') return ReadObject(text, ref index);
            if (c == '[') return ReadArray(text, ref index);
            if (c == '"') return ReadString(text, ref index);
            if (Match(text, ref index, "true")) return true;
            if (Match(text, ref index, "false")) return false;
            if (Match(text, ref index, "null")) return null;
            return ReadNumber(text, ref index);
        }

        static bool Match(string text, ref int index, string word)
        {
            if (string.CompareOrdinal(text, index, word, 0, word.Length) != 0) return false;
            index += word.Length;
            return true;
        }

        static Dictionary<string, object> ReadObject(string text, ref int index)
        {
            var map = new Dictionary<string, object>();
            index++;
            SkipSpace(text, ref index);
            if (index < text.Length && text[index] == '}')
            {
                index++;
                return map;
            }
            while (true)
            {
                SkipSpace(text, ref index);
                if (index >= text.Length || text[index] != '"') throw new FormatException("expected a key at " + index);
                string key = ReadString(text, ref index);
                SkipSpace(text, ref index);
                if (index >= text.Length || text[index] != ':') throw new FormatException("expected ':' at " + index);
                index++;
                map[key] = ReadValue(text, ref index);
                SkipSpace(text, ref index);
                if (index >= text.Length) throw new FormatException("unexpected end");
                if (text[index] == ',')
                {
                    index++;
                    continue;
                }
                if (text[index] == '}')
                {
                    index++;
                    return map;
                }
                throw new FormatException("expected ',' or '}' at " + index);
            }
        }

        static List<object> ReadArray(string text, ref int index)
        {
            var list = new List<object>();
            index++;
            SkipSpace(text, ref index);
            if (index < text.Length && text[index] == ']')
            {
                index++;
                return list;
            }
            while (true)
            {
                list.Add(ReadValue(text, ref index));
                SkipSpace(text, ref index);
                if (index >= text.Length) throw new FormatException("unexpected end");
                if (text[index] == ',')
                {
                    index++;
                    continue;
                }
                if (text[index] == ']')
                {
                    index++;
                    return list;
                }
                throw new FormatException("expected ',' or ']' at " + index);
            }
        }

        static string ReadString(string text, ref int index)
        {
            var value = new StringBuilder();
            index++;
            while (index < text.Length)
            {
                char c = text[index++];
                if (c == '"') return value.ToString();
                if (c != '\\')
                {
                    value.Append(c);
                    continue;
                }
                if (index >= text.Length) break;
                char escape = text[index++];
                switch (escape)
                {
                    case '"': value.Append('"'); break;
                    case '\\': value.Append('\\'); break;
                    case '/': value.Append('/'); break;
                    case 'b': value.Append('\b'); break;
                    case 'f': value.Append('\f'); break;
                    case 'n': value.Append('\n'); break;
                    case 'r': value.Append('\r'); break;
                    case 't': value.Append('\t'); break;
                    case 'u':
                        if (index + 4 > text.Length) throw new FormatException("bad \\u escape");
                        value.Append((char)int.Parse(text.Substring(index, 4), NumberStyles.HexNumber,
                                                     CultureInfo.InvariantCulture));
                        index += 4;
                        break;
                    default: throw new FormatException("bad escape \\" + escape);
                }
            }
            throw new FormatException("unterminated string");
        }

        static object ReadNumber(string text, ref int index)
        {
            int start = index;
            while (index < text.Length && "+-0123456789.eE".IndexOf(text[index]) >= 0) index++;
            if (index == start) throw new FormatException("unexpected '" + text[index] + "' at " + index);
            double number;
            if (!double.TryParse(text.Substring(start, index - start), NumberStyles.Float,
                                 CultureInfo.InvariantCulture, out number))
                throw new FormatException("bad number at " + start);
            return number;
        }
    }
}
