using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using Microsoft.Win32;
using System.Windows.Forms;

namespace VideoLocalizerPortable
{
    internal static class Program
    {
        [STAThread]
        private static void Main()
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new LauncherForm());
        }
    }

    internal sealed class LauncherForm : Form
    {
        private readonly TextBox logBox = new TextBox();
        private readonly Label urlLabel = new Label();
        private readonly Button openButton = new Button();
        private readonly Button folderButton = new Button();
        private readonly Button stopButton = new Button();
        private Process server;
        private string root;
        private string url;
        private string logPath;

        public LauncherForm()
        {
            Text = "Video Localizer 本地视频双字幕工具";
            Width = 760;
            Height = 520;
            MinimumSize = new Size(640, 420);
            StartPosition = FormStartPosition.CenterScreen;
            Font = new Font("Segoe UI", 10F);

            var title = new Label { Text = "Video Localizer", Font = new Font("Segoe UI Semibold", 20F), AutoSize = true, Left = 24, Top = 18 };
            var subtitle = new Label { Text = "正在检测电脑配置并启动本地服务…", AutoSize = true, Left = 27, Top = 60, ForeColor = Color.DimGray };
            urlLabel.SetBounds(27, 92, 690, 28);
            urlLabel.Font = new Font("Consolas", 11F, FontStyle.Bold);
            urlLabel.ForeColor = Color.FromArgb(31, 96, 180);
            urlLabel.Text = "URL：等待服务启动";

            openButton.Text = "打开控制面板";
            openButton.SetBounds(27, 128, 145, 36);
            openButton.Enabled = false;
            openButton.Click += (s, e) => OpenUrl();

            folderButton.Text = "打开程序目录";
            folderButton.SetBounds(182, 128, 135, 36);
            folderButton.Click += (s, e) => Process.Start("explorer.exe", root ?? AppDomain.CurrentDomain.BaseDirectory);

            stopButton.Text = "停止服务";
            stopButton.SetBounds(327, 128, 110, 36);
            stopButton.Enabled = false;
            stopButton.Click += (s, e) => StopServer();

            logBox.SetBounds(27, 180, 690, 270);
            logBox.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
            logBox.Multiline = true;
            logBox.ReadOnly = true;
            logBox.ScrollBars = ScrollBars.Vertical;
            logBox.BackColor = Color.FromArgb(248, 249, 251);
            logBox.Font = new Font("Consolas", 9.5F);

            Controls.AddRange(new Control[] { title, subtitle, urlLabel, openButton, folderButton, stopButton, logBox });
            Shown += (s, e) => ThreadPool.QueueUserWorkItem(_ => StartPortableServer());
            FormClosing += (s, e) => { if (server != null && !server.HasExited) { var result = MessageBox.Show("关闭启动器并停止本地服务？", "Video Localizer", MessageBoxButtons.YesNo, MessageBoxIcon.Question); if (result == DialogResult.No) e.Cancel = true; else StopServer(); } };
        }

        private void Append(string message)
        {
            var line = "[" + DateTime.Now.ToString("HH:mm:ss") + "] " + message;
            try { if (!String.IsNullOrEmpty(logPath)) File.AppendAllText(logPath, line + Environment.NewLine, new UTF8Encoding(false)); } catch { }
            if (IsHandleCreated) BeginInvoke((Action)(() => { logBox.AppendText(line + Environment.NewLine); logBox.SelectionStart = logBox.TextLength; logBox.ScrollToCaret(); }));
        }

        private void StartPortableServer()
        {
            root = Path.GetFullPath(AppDomain.CurrentDomain.BaseDirectory);
            var runtime = Path.Combine(root, "runtime");
            var python = Path.Combine(runtime, "python.exe");
            var app = Path.Combine(root, "app", "local_app_v2.py");
            var tools = Path.Combine(root, "tools");
            var data = Path.Combine(root, "data");
            var logs = Path.Combine(root, "logs");
            Directory.CreateDirectory(data);
            Directory.CreateDirectory(logs);
            Directory.CreateDirectory(Path.Combine(data, "downloads"));
            Directory.CreateDirectory(Path.Combine(data, "output"));
            Directory.CreateDirectory(Path.Combine(data, "local_jobs"));
            logPath = Path.Combine(logs, "launcher_" + DateTime.Now.ToString("yyyyMMdd") + ".log");

            Append("系统：" + DetectWindows() + "，64位进程：" + Environment.Is64BitProcess);
            if (!Environment.Is64BitOperatingSystem) { Fail("仅支持 Windows 10/11 64 位系统。"); return; }
            if (!File.Exists(python)) { Fail("便携 Python 运行时缺失：" + python); return; }
            if (!File.Exists(app)) { Fail("程序后端缺失：" + app); return; }
            if (!File.Exists(Path.Combine(tools, "ffmpeg.exe")) || !File.Exists(Path.Combine(tools, "ffprobe.exe"))) { Fail("FFmpeg 工具缺失，请重新解压完整发行包。"); return; }
            if (!Directory.Exists(Path.Combine(root, "app", "test_run_dl", "models", "faster-whisper-tiny"))) { Fail("本地语音识别模型缺失，请重新解压完整发行包。"); return; }

            var gpu = DetectNvidia(Path.Combine(tools, "nvidia-smi.exe"));
            Append(gpu.Length == 0 ? "GPU：未检测到可用 NVIDIA GPU，将自动使用 CPU 兼容模式。" : "GPU：" + gpu);
            Append("FFmpeg：便携版本已找到。");

            if (WaitForHealth("http://127.0.0.1:8790/api/health", 1, false))
            {
                url = "http://127.0.0.1:8790/";
                Append("检测到本程序已在运行，直接复用现有服务：" + url);
                BeginInvoke((Action)(() => { urlLabel.Text = "URL：" + url; openButton.Enabled = true; stopButton.Enabled = false; }));
                OpenUrl();
                return;
            }
            var port = FindAvailablePort(8790, 8899);
            if (port == 0) { Fail("8790–8899 端口均被占用，无法启动服务。"); return; }
            url = "http://127.0.0.1:" + port + "/";

            var psi = new ProcessStartInfo
            {
                FileName = python,
                Arguments = "-u \"" + app + "\"",
                WorkingDirectory = Path.Combine(root, "app"),
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            psi.EnvironmentVariables["VIDEO_LOCALIZER_PORT"] = port.ToString();
            psi.EnvironmentVariables["VIDEO_LOCALIZER_PORTABLE_ROOT"] = root.TrimEnd(Path.DirectorySeparatorChar);
            psi.EnvironmentVariables["VIDEO_LOCALIZER_DATA_DIR"] = data;
            psi.EnvironmentVariables["VIDEO_LOCALIZER_RUNTIME_ROOT"] = runtime;
            psi.EnvironmentVariables["VIDEO_LOCALIZER_FORCE_CPU"] = gpu.Length == 0 ? "1" : "0";
            psi.EnvironmentVariables["PATH"] = tools + ";" + runtime + ";" + Path.Combine(runtime, "Scripts") + ";" + psi.EnvironmentVariables["PATH"];
            var envFile = Path.Combine(root, "config", ".env.local");
            if (File.Exists(envFile)) psi.EnvironmentVariables["VIDEO_LOCALIZER_ENV_FILE"] = envFile;

            try
            {
                server = new Process { StartInfo = psi, EnableRaisingEvents = true };
                server.OutputDataReceived += (s, e) => { if (e.Data != null) Append("服务：" + e.Data); };
                server.ErrorDataReceived += (s, e) => { if (e.Data != null) Append("服务：" + e.Data); };
                server.Exited += (s, e) => { Append("本地服务已退出，代码：" + server.ExitCode); BeginInvoke((Action)(() => { openButton.Enabled = false; stopButton.Enabled = false; })); };
                server.Start();
                server.BeginOutputReadLine();
                server.BeginErrorReadLine();
                Append("服务进程已启动，PID=" + server.Id + "，正在等待健康检查…");
            }
            catch (Exception ex) { Fail("无法启动本地服务：" + ex.Message); return; }

            if (!WaitForHealth(url + "api/health", 45, true)) { Fail("服务在45秒内未就绪。请查看日志：" + logPath); return; }
            Append("启动成功。控制面板 URL：" + url);
            BeginInvoke((Action)(() => { urlLabel.Text = "URL：" + url; openButton.Enabled = true; stopButton.Enabled = true; }));
            OpenUrl();
        }

        private string DetectWindows()
        {
            try
            {
                using (var key = Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\Windows NT\CurrentVersion"))
                {
                    var name = Convert.ToString(key.GetValue("ProductName"));
                    var display = Convert.ToString(key.GetValue("DisplayVersion"));
                    var build = Convert.ToString(key.GetValue("CurrentBuildNumber"));
                    if (!String.IsNullOrWhiteSpace(name)) return name + (String.IsNullOrWhiteSpace(display) ? "" : " " + display) + (String.IsNullOrWhiteSpace(build) ? "" : " (Build " + build + ")");
                }
            }
            catch { }
            return Environment.OSVersion.ToString();
        }

        private string DetectNvidia(string bundledSmi)
        {
            var candidates = new[] { bundledSmi, Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "nvidia-smi.exe"), Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "NVIDIA Corporation", "NVSMI", "nvidia-smi.exe") };
            foreach (var candidate in candidates.Where(File.Exists))
            {
                try
                {
                    var p = Process.Start(new ProcessStartInfo { FileName = candidate, Arguments = "--query-gpu=name,memory.total,driver_version --format=csv,noheader", UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true });
                    var output = p.StandardOutput.ReadToEnd().Trim(); p.WaitForExit(5000); if (p.ExitCode == 0 && output.Length > 0) return output.Replace("\n", "; ");
                }
                catch { }
            }
            return "";
        }

        private int FindAvailablePort(int start, int end)
        {
            for (var port = start; port <= end; port++)
            {
                try { var listener = new TcpListener(IPAddress.Loopback, port); listener.Start(); listener.Stop(); return port; }
                catch (SocketException) { }
            }
            return 0;
        }

        private bool WaitForHealth(string healthUrl, int seconds, bool monitorServer)
        {
            for (var i = 0; i < seconds * 2; i++)
            {
                if (monitorServer && server != null && server.HasExited) return false;
                try { var request = WebRequest.Create(healthUrl); request.Timeout = 1500; using (var response = request.GetResponse()) return true; }
                catch { Thread.Sleep(500); }
            }
            return false;
        }

        private void OpenUrl()
        {
            if (String.IsNullOrEmpty(url)) return;
            try { Process.Start(new ProcessStartInfo(url) { UseShellExecute = true }); }
            catch (Exception ex) { Append("浏览器打开失败，请复制 URL：" + ex.Message); }
        }

        private void StopServer()
        {
            try { if (server != null && !server.HasExited) { server.Kill(); server.WaitForExit(5000); Append("本地服务已停止。"); } }
            catch (Exception ex) { Append("停止服务失败：" + ex.Message); }
        }

        private void Fail(string message)
        {
            Append("启动失败：" + message);
            if (IsHandleCreated) BeginInvoke((Action)(() => { urlLabel.Text = "启动失败，请查看下方日志"; MessageBox.Show(message, "Video Localizer 启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error); }));
        }
    }
}
