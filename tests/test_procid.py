"""Tests for ains_runtime/procid.py — process identity and the tree stop.

procid.py is what makes "a pid alone is never trusted" true: identity is
pid + start time + argv, zombies count as gone, and cancellation signals
every distinct process group among the engine and its descendants. The
suite's end-to-end launches (test_runtime_exec.py) exercise only the Linux
/proc method — CI's unit job is ubuntu-latest — so the `ps-lstart` fallback
every non-Linux host takes, and the guards against hostile inputs, had no
coverage at all (74% at ba4b973). These tests drive the module directly:

  * the `ps` path, by forcing `_proc_usable()` False and pointing
    `_ps_path()` at a fake `ps` executable (a real subprocess, so
    `_run_ps`' env forcing and error handling run for real) or by feeding
    `_run_ps` fabricated stdout — the pattern test_doctor_parsers.py uses
    for doctor's engines
  * the /proc path's guards and zombie semantics, with real short-lived
    children (every wait bounded; tearDown SIGKILLs anything left)
  * the tree helpers against synthetic Proc tables, including the cycle
    that /proc cannot produce but a defensive walk must survive

No production code is changed.
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runtime_util import REPO, kill_matching, wait_for  # noqa: E402

sys.path.insert(0, str(REPO / "skills/investigation/scripts"))
from ains_runtime import procid  # noqa: E402

HAVE_PROC = sys.platform.startswith("linux") and Path("/proc/self/stat").is_file()


def write_ps(path: Path, stdout: str, rc: int = 0) -> Path:
    """A fake `ps` that prints stdout and exits rc (a real subprocess)."""
    path.write_text("#!/bin/sh\ncat <<'EOF'\n%s\nEOF\nexit %d\n" % (stdout, rc))
    path.chmod(0o755)
    return path


def ps_identity_stdout(stat="S", lstart="Mon Nov 12 08:00:00 2026",
                       command="/usr/bin/python3 /x/eng.py run c.yaml") -> str:
    """`ps -ww -o stat= -o lstart= -o command= -p <pid>` output: stat, five
    lstart tokens (weekday mon day hh:mm:ss year), then the command line."""
    return f"{stat}   {lstart}   {command}"


def ps_table_stdout() -> str:
    """A `ps -ax -o pid=,ppid=,pgid=,stat=` table with junk lines, a
    non-numeric pid and a zombie that the parser must skip."""
    return ("  100  1  100 S\n"
            "  101  100  100 S\n"
            "  102  100  105 S\n"
            "  200  1  200 R\n"
            "garbage without numbers\n"
            "  notapid 1 1 S\n"
            "  203  1  203 Z+\n")


class PsPath:
    """Force the ps-lstart method with `_ps_path` -> path (None: no ps)."""

    def __init__(self, path):
        self.patchers = [
            mock.patch.object(procid, "_proc_usable", return_value=False),
            mock.patch.object(procid, "_ps_path", return_value=path),
        ]

    def __enter__(self):
        for p in self.patchers:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patchers:
            p.stop()
        return False


class ProcIdTest(unittest.TestCase):
    """Shared setUp: tempdir, bounded child cleanup."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.children: list[subprocess.Popen] = []
        self._boot_id = procid._BOOT_ID

    def tearDown(self):
        # Spawned children first, then anything whose command line carries
        # this test's temp-dir name (grandchildren started with
        # start_new_session=True would otherwise outlive a failed test).
        for p in self.children:
            if p.poll() is None:
                p.kill()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        kill_matching(self.tmp.name)
        procid._BOOT_ID = self._boot_id
        shutil.rmtree(self.tmp, ignore_errors=True)

    def spawn(self, argv=None, **kwargs):
        argv = argv or [sys.executable, "-c", "import time; time.sleep(30)"]
        p = subprocess.Popen(argv, start_new_session=True, **kwargs)
        self.children.append(p)
        return p

    def alive(self, pid):
        return procid.identity(pid) is not None


# --- method selection and launch blocking ------------------------------------

class Method(ProcIdTest):
    def test_proc_first_then_ps_then_none(self):
        # /proc usable -> proc-stat, no `ps` consulted.
        with mock.patch.object(procid, "_proc_usable", return_value=True):
            self.assertEqual(procid.method(), "proc-stat")
        # /proc unusable, ps present -> ps-lstart.
        with PsPath("/fake/ps"):
            self.assertEqual(procid.method(), "ps-lstart")
        # Neither -> None, and require() blocks launch with the reason.
        with PsPath(None):
            self.assertIsNone(procid.method())
            with self.assertRaises(procid.ProcIdError) as cm:
                procid.require()
            self.assertIn("/proc", str(cm.exception))
            self.assertIn("PATH", str(cm.exception))
            self.assertIn("blocked", str(cm.exception))

    def test_require_proves_ps_works_for_the_current_process(self):
        # ps on PATH but it answers nothing for us: launch is blocked.
        with PsPath("/fake/ps"), \
                mock.patch.object(procid, "_run_ps", return_value=""):
            with self.assertRaises(procid.ProcIdError) as cm:
                procid.require()
            self.assertIn("no start time", str(cm.exception))
        # And with a working ps it returns the method.
        with PsPath("/fake/ps"), \
                mock.patch.object(procid, "_run_ps",
                                  return_value=ps_identity_stdout() + "\n"):
            self.assertEqual(procid.require(), "ps-lstart")

    def test_ps_env_forces_c_locale_and_utc(self):
        env = procid._ps_env()
        self.assertEqual((env["LC_ALL"], env["LANG"], env["TZ"]), ("C", "C", "UTC"))
        # The caller's environment is inherited, not replaced.
        for k in os.environ:
            self.assertIn(k, env)

    def test_run_ps_reports_failure_as_none(self):
        ps = write_ps(self.tmp / "ps", "whatever", rc=3)
        with PsPath(str(ps)):
            self.assertIsNone(procid._run_ps(["-x"]))
        # A ps that cannot be executed at all, and one that times out.
        for exc in (OSError("no exec"), subprocess.SubprocessError("timed out")):
            with PsPath("/fake/ps"), \
                    mock.patch.object(subprocess, "run", side_effect=exc):
                self.assertIsNone(procid._run_ps(["-x"]))

    def test_run_ps_without_ps_on_path_is_none(self):
        with mock.patch.object(procid, "_ps_path", return_value=None):
            self.assertIsNone(procid._run_ps(["-x"]))


# --- identity: the ps-lstart fallback ----------------------------------------

class PsIdentity(ProcIdTest):
    def test_identity_parses_stat_lstart_and_command(self):
        with PsPath("/fake/ps"), \
                mock.patch.object(procid, "_run_ps",
                                  return_value=ps_identity_stdout() + "\n"):
            ident = procid.identity(4242)
        self.assertEqual(ident.pid, 4242)
        self.assertEqual(ident.start, "ps-lstart:Mon Nov 12 08:00:00 2026")
        self.assertEqual(ident.cmdline, "/usr/bin/python3 /x/eng.py run c.yaml")
        self.assertFalse(ident.zombie)

    def test_identity_runs_the_real_ps_subprocess(self):
        # Through _run_ps, not a mocked return: a genuine child process with
        # the forced C/UTC environment.
        ps = write_ps(self.tmp / "ps", ps_identity_stdout())
        with PsPath(str(ps)):
            self.assertEqual(procid.require(), "ps-lstart")
            ident = procid.identity(4242)
        self.assertEqual(ident.start, "ps-lstart:Mon Nov 12 08:00:00 2026")

    def test_identity_zombie_counts_as_gone_unless_asked(self):
        with PsPath("/fake/ps"), \
                mock.patch.object(procid, "_run_ps",
                                  return_value=ps_identity_stdout(stat="Z") + "\n"):
            self.assertIsNone(procid.identity(4242))
            self.assertIsNone(procid.start_time(4242))
            ident = procid.identity(4242, include_zombie=True)
            self.assertTrue(ident.zombie)
            self.assertEqual(ident.start, "ps-lstart:Mon Nov 12 08:00:00 2026")

    def test_identity_rejects_unusable_ps_output(self):
        for out in ("", "   \n", "R Mon\n", None):
            with PsPath("/fake/ps"), \
                    mock.patch.object(procid, "_run_ps", return_value=out):
                self.assertIsNone(procid.identity(4242), repr(out))

    def test_identity_rejects_bad_pids(self):
        with PsPath("/fake/ps"), \
                mock.patch.object(procid, "_run_ps",
                                  return_value=ps_identity_stdout() + "\n"):
            for bad in (0, -1, "x", 3.5, None):
                self.assertIsNone(procid.identity(bad), repr(bad))
        # No usable method at all (neither /proc nor ps): never a pid-only
        # identity.
        with mock.patch.object(procid, "method", return_value=None):
            self.assertIsNone(procid.identity(os.getpid()))

    def test_matches_on_the_ps_path(self):
        argv = ["/usr/bin/python3", "/x/eng.py", "run", "c.yaml"]
        out = ps_identity_stdout(command=" ".join(argv)) + "\n"
        # matches() re-reads identity(), so the ps stub must stay in force.
        with PsPath("/fake/ps"), mock.patch.object(procid, "_run_ps", return_value=out):
            ident = procid.identity(4242)
            self.assertTrue(procid.matches(4242, ident.start, argv))
            # A different start time or argv is a different process.
            self.assertFalse(procid.matches(4242, ident.start + "!", argv))
            self.assertFalse(procid.matches(4242, ident.start,
                                            ["/usr/bin/python3", "/x/eng.py", "run",
                                             "d.yaml"]))
            # argv None: identity by pid + start only.
            self.assertTrue(procid.matches(4242, ident.start, None))
        # A missing pid or start never matches.
        self.assertFalse(procid.matches(None, "s", None))
        self.assertFalse(procid.matches(4242, None, None))


# --- identity: the /proc path's guards ---------------------------------------

@unittest.skipUnless(HAVE_PROC, "needs /proc/<pid>/stat")
class ProcIdentity(ProcIdTest):
    def test_identity_of_a_live_process(self):
        p = self.spawn()
        wait_for(lambda: self.alive(p.pid), what="child alive")
        ident = procid.identity(p.pid)
        self.assertTrue(ident.start.startswith("proc-stat:"))
        self.assertEqual(ident.pid, p.pid)
        self.assertIn("python", ident.cmdline)
        self.assertEqual(procid.start_time(p.pid), ident.start)
        self.assertTrue(procid.matches(p.pid, ident.start, None))

    def test_a_zombie_counts_as_gone(self):
        # A helper spawns a child that exits and never reaps it: the child
        # stays a zombie, which must read as gone by default and visible
        # (same start time) with include_zombie — the supervisor relies on
        # exactly this when it records the engine pid after a fast exit.
        helper = self.spawn([sys.executable, "-c",
                             "import subprocess, sys, time\n"
                             "subprocess.Popen([sys.executable, '-c', 'pass'])\n"
                             "time.sleep(30)\n"])
        wait_for(lambda: self.alive(helper.pid), what="helper alive")
        zombie = None

        def find_zombie():
            nonlocal zombie
            for pid in os.listdir("/proc"):
                if pid.isdigit():
                    ident = procid.identity(int(pid), include_zombie=True)
                    if ident and ident.zombie:
                        zombie = ident
                        return ident
            return None

        wait_for(find_zombie, what="a zombie to appear")
        self.assertIsNone(procid.identity(zombie.pid))
        self.assertIsNone(procid.start_time(zombie.pid))
        st = procid.start_time(zombie.pid, include_zombie=True)
        self.assertEqual(st, zombie.start)
        # matches() follows identity(): a zombie is not a live engine.
        self.assertFalse(procid.matches(zombie.pid, zombie.start, None))

    def test_stat_that_cannot_be_parsed_is_no_identity(self):
        me = os.getpid()
        for fields in (None, ["S", "1", "100"]):
            with mock.patch.object(procid, "_proc_stat_fields", return_value=fields):
                self.assertIsNone(procid.identity(me))
        with mock.patch.object(Path, "read_text", return_value="123 S 1 2"):
            self.assertIsNone(procid._proc_stat_fields(123))
        with mock.patch.object(procid, "_proc_cmdline", return_value=None):
            # No cmdline is still an identity: the start time carries it.
            ident = procid.identity(me)
            self.assertEqual(ident.cmdline, "")

    def test_boot_id_falls_back_when_unreadable(self):
        with mock.patch.object(procid, "_BOOT_ID", None), \
                mock.patch.object(Path, "read_text", side_effect=OSError("denied")):
            self.assertEqual(procid._boot_id(), "unknown-boot")

    def test_cmdline_that_vanishes_mid_read_is_none(self):
        # A pid whose /proc/<pid>/cmdline disappears between the stat read
        # and the cmdline read: no cmdline, still an identity.
        with mock.patch.object(Path, "read_bytes", side_effect=OSError("gone")):
            self.assertIsNone(procid._proc_cmdline(os.getpid()))
        self.assertIsInstance(procid._ps_path(), (str, type(None)))

    def test_process_table_lists_live_processes_only(self):
        tbl = procid.process_table()
        self.assertGreater(len(tbl), 0)
        self.assertIn(os.getpid(), [p.pid for p in tbl])
        self.assertTrue(all(isinstance(p.pgid, int) for p in tbl))
        # A dead pid is never in the table.
        dead = self.spawn([sys.executable, "-c", "pass"])
        dead.wait(timeout=5)
        self.assertNotIn(dead.pid, [p.pid for p in procid.process_table()])


# --- argv matching -----------------------------------------------------------

class Argv(ProcIdTest):
    def test_exact_and_python_launcher_equivalence(self):
        argv = ["/usr/local/bin/python3", "/x/eng.py", "run", "c.yaml"]
        self.assertTrue(procid.argv_matches(" ".join(argv), argv))
        # Interpreter name may differ (python3 vs python3.11 vs framework Python).
        self.assertTrue(procid.argv_matches(
            "/usr/bin/python3.11 /x/eng.py run c.yaml", argv))
        self.assertTrue(procid.argv_matches(
            "/.../Python.app/Contents/MacOS/Python /x/eng.py run c.yaml", argv))
        # Whitespace is normalized on both sides.
        self.assertTrue(procid.argv_matches(
            "/usr/bin/python3  /x/eng.py    run c.yaml", argv))

    def test_a_different_program_or_arguments_do_not_match(self):
        argv = ["/usr/local/bin/python3", "/x/eng.py", "run", "c.yaml"]
        self.assertFalse(procid.argv_matches("/usr/bin/sleep 30", argv))
        self.assertFalse(procid.argv_matches(
            "/usr/local/bin/python3 /x/eng.py run d.yaml", argv))
        # Non-python programs are not interchangeable.
        self.assertFalse(procid.argv_matches("/usr/bin/other /x/eng.py run c.yaml", argv))

    def test_single_argument_argv_matches_by_basename(self):
        self.assertTrue(procid.argv_matches("/usr/bin/sleep", ["/usr/bin/sleep"]))
        self.assertTrue(procid.argv_matches("/any/path/sleep", ["/usr/bin/sleep"]))
        self.assertFalse(procid.argv_matches("/usr/bin/other", ["/usr/bin/sleep"]))

    def test_empty_sides_never_match(self):
        self.assertFalse(procid.argv_matches("", ["a"]))
        self.assertFalse(procid.argv_matches("a b", None))
        self.assertFalse(procid.argv_matches("a b", []))


# --- find_by_argv: recovery's launch evidence --------------------------------

class FindByArgv(ProcIdTest):
    PS_FIND = ("  200 S /usr/bin/python3 /x/eng.py run c.yaml --run-id r1\n"
               "  201 Z /usr/bin/python3 /x/eng.py run c.yaml --run-id r1\n"
               "  202 R /usr/bin/sleep 30\n"
               "  204 R short\n")

    def test_ps_path_finds_live_matches_only(self):
        argv = ["/usr/bin/python3", "/x/eng.py", "run", "c.yaml", "--run-id", "r1"]
        with PsPath("/fake/ps"), \
                mock.patch.object(procid, "_run_ps", return_value=self.PS_FIND):
            self.assertEqual(procid.find_by_argv(argv), [200])  # zombie 201 excluded
            # Empty or absent argv is never a search.
            self.assertEqual(procid.find_by_argv([]), [])
            self.assertEqual(procid.find_by_argv(None), [])

    # Issue #23: a process whose argv contains an embedded newline makes `ps`
    # print a continuation line whose first token is not a pid. process_table()'s
    # ps path skips those lines (int() guarded by except ValueError);
    # find_by_argv's does not, so one malformed line makes find_by_argv raise
    # ValueError out of recovery.evidence() (which catches only ProcIdError)
    # into reconcile()/status()/cancel()/mark_not_launched() — exactly the
    # recovery flow the module exists to keep honest. Pinned as an expected
    # failure: it flips to "unexpected success" (a failing run) the moment
    # the guard lands, then it asserts the fixed behaviour.
    @unittest.expectedFailure
    def test_ps_path_skips_non_numeric_first_tokens(self):
        # Three tokens with a matching command column: this line reaches the
        # unguarded int(t[0]) today (a one-token continuation is already
        # skipped by the len(t) == 3 check, so it cannot pin the defect).
        continuation = "line2 S /usr/bin/python3 /x/eng.py run c.yaml --run-id r1"
        text = f"  200 S /usr/bin/python3 /x/eng.py run c.yaml --run-id r1\n{continuation}\n"
        argv = ["/usr/bin/python3", "/x/eng.py", "run", "c.yaml", "--run-id", "r1"]
        with PsPath("/fake/ps"), \
                mock.patch.object(procid, "_run_ps", return_value=text):
            # Once the guard exists, a junk line is skipped, not fatal —
            # the same contract process_table()'s ps path already keeps.
            self.assertEqual(procid.find_by_argv(argv), [200])

    @unittest.skipUnless(HAVE_PROC, "needs /proc")
    def test_proc_path_finds_and_excludes_self_and_the_dead(self):
        argv = [sys.executable, "-c", "import time; time.sleep(30)  # procid-marker"]
        p = self.spawn(argv)
        wait_for(lambda: self.alive(p.pid), what="child alive")
        self.assertIn(p.pid, procid.find_by_argv(argv))
        # The searching process itself is never reported.
        self.assertNotIn(os.getpid(), procid.find_by_argv(
            [sys.executable, "-c", "import sys; sys.exit(0)"]))
        # A different argv does not match this child.
        self.assertNotIn(p.pid, procid.find_by_argv(
            [sys.executable, "-c", "import time; time.sleep(30)  # other"]))
        p.kill()
        p.wait(timeout=5)
        self.assertNotIn(p.pid, procid.find_by_argv(argv))


# --- process table, descendants, groups --------------------------------------

class TreeHelpers(ProcIdTest):
    def setUp(self):
        super().setUp()
        # Two groups under pid 10 (10, 20) plus an unrelated process; 16/17
        # form a cycle /proc cannot produce but a defensive walk must survive.
        self.table = [
            procid.Proc(10, 1, 10),
            procid.Proc(11, 10, 10),
            procid.Proc(12, 10, 20),
            procid.Proc(13, 11, 10),
            procid.Proc(14, 12, 20),
            procid.Proc(15, 99, 30),
            procid.Proc(16, 17, 40),
            procid.Proc(17, 16, 40),
        ]

    def test_descendants_walks_children_without_root(self):
        # Order is not specified (a dict-of-children walk); membership is.
        self.assertEqual(sorted(p.pid for p in procid.descendants(10, self.table)),
                         [11, 12, 13, 14])
        self.assertEqual([p.pid for p in procid.descendants(15, self.table)], [])
        # The cycle terminates: 16's only child 17 is reported (17's parent
        # link back to 16 is the root, which is never its own descendant).
        self.assertEqual([p.pid for p in procid.descendants(16, self.table)], [17])
        self.assertEqual([p.pid for p in procid.descendants(17, self.table)], [16])

    def test_tree_groups_collects_distinct_groups_of_the_tree(self):
        self.assertEqual(procid.tree_groups(10, root_pgid=10, table=self.table),
                         [10, 20])
        # A recorded group whose members were reparented: their descendants'
        # groups are included too (18 is in group 20, its child 19 in 50).
        table = self.table + [procid.Proc(18, 15, 20), procid.Proc(19, 18, 50)]
        self.assertEqual(procid.tree_groups(10, root_pgid=10, table=table),
                         [10, 20, 50])
        # A dead root still reports its recorded group.
        self.assertEqual(procid.tree_groups(999, root_pgid=55, table=table), [55])
        # Protected groups (init, self's group) are never signalled.
        self.assertNotIn(0, procid.tree_groups(10, root_pgid=10, table=self.table))

    def test_group_members_lists_pids_in_the_groups(self):
        self.assertEqual(procid.group_members([10, 20], self.table),
                         [10, 11, 12, 13, 14])
        self.assertEqual(procid.group_members([], self.table), [])

    def test_protected_groups_survive_getpgrp_failure(self):
        with mock.patch.object(procid.os, "getpgrp", side_effect=OSError()):
            self.assertEqual(procid._protected_groups(), {0, 1})
        self.assertEqual(procid._protected_groups(), {0, 1, os.getpgrp()})

    def test_ps_path_process_table_filters_junk_and_zombies(self):
        with PsPath("/fake/ps"), \
                mock.patch.object(procid, "_run_ps", return_value=ps_table_stdout()):
            self.assertEqual([(p.pid, p.ppid, p.pgid) for p in procid.process_table()],
                             [(100, 1, 100), (101, 100, 100), (102, 100, 105),
                              (200, 1, 200)])

    def test_ps_path_process_table_without_ps_raises(self):
        with PsPath("/fake/ps"), \
                mock.patch.object(procid, "_run_ps", return_value=None):
            with self.assertRaises(procid.ProcIdError):
                procid.process_table()


# --- signalling --------------------------------------------------------------

class Signalling(ProcIdTest):
    def test_signal_groups_reports_each_result(self):
        def killpg(g, sig):
            if g == 500:
                raise ProcessLookupError()
            if g == 501:
                raise PermissionError()
        with mock.patch.object(procid.os, "getpgrp", return_value=7), \
                mock.patch.object(procid.os, "killpg", side_effect=killpg):
            res = procid.signal_groups([500, 501, 502, 0, 1, 7, 502], signal.SIGTERM)
        # Protected groups (0, 1, ours) and duplicates never signalled.
        self.assertEqual([r["pgid"] for r in res], [500, 501, 502])
        self.assertEqual([r["result"] for r in res],
                         ["no such group", "permission denied", "sent"])
        self.assertEqual([r["signal"] for r in res], ["SIGTERM"] * 3)

    def test_signal_if_match_only_signals_the_named_process(self):
        # The child writes a marker file on SIGUSR1 and another once its
        # handlers are installed (signalling before that would terminate it).
        marker, armed = self.tmp / "usr1", self.tmp / "armed"
        code = ("import signal, sys, time\n"
                "path, armed = sys.argv[1], sys.argv[2]\n"
                "def hit(*a):\n"
                "    open(path, 'w').write('usr1')\n"
                "signal.signal(signal.SIGUSR1, hit)\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "open(armed, 'w').write('1')\n"
                "time.sleep(30)\n")
        p = self.spawn([sys.executable, "-c", code, str(marker), str(armed)])
        wait_for(lambda: armed.exists(), what="handlers armed")
        start = procid.start_time(p.pid)
        self.assertTrue(procid.signal_if_match(p.pid, start, None, signal.SIGUSR1))
        self.assertEqual(wait_for(lambda: marker.exists() and marker.read_text(),
                                  what="marker written"), "usr1")
        # A mismatched identity or a dead pid sends nothing.
        self.assertFalse(procid.signal_if_match(p.pid, "proc-stat:x:1", None,
                                                signal.SIGUSR1))
        self.assertFalse(procid.signal_if_match(999999, "proc-stat:x:1", None,
                                                signal.SIGUSR1))
        self.assertTrue(self.alive(p.pid))
        # A kill that fails reports not-sent instead of raising.
        for exc in (ProcessLookupError(), PermissionError()):
            with mock.patch.object(procid.os, "kill", side_effect=exc):
                self.assertFalse(procid.signal_if_match(p.pid, start, None,
                                                        signal.SIGUSR1))


# --- stop_process_groups: the cancellation mechanism --------------------------

class StopProcessGroups(ProcIdTest):
    def test_no_engine_pid_is_an_error_not_a_stop(self):
        for pid in (None, 0):
            res = procid.stop_process_groups(pid, None, None)
            self.assertEqual(res, {"mechanism": "process-groups", "requested": False,
                                   "groups": [], "signalled": [], "survivors": [],
                                   "error": "no engine pid recorded"})

    def test_reused_pid_is_never_signalled(self):
        other = self.spawn()
        wait_for(lambda: self.alive(other.pid), what="helper alive")
        res = procid.stop_process_groups(other.pid, "proc-stat:bogus:123", None)
        self.assertFalse(res["requested"])
        self.assertEqual(res["signalled"], [])
        self.assertIn("different process", res["error"])
        self.assertTrue(self.alive(other.pid))

    def test_process_table_failure_is_reported_not_raised(self):
        with mock.patch.object(procid, "process_table",
                               side_effect=procid.ProcIdError("no ps")):
            res = procid.stop_process_groups(1234, "x", None)
        self.assertFalse(res["requested"])
        self.assertEqual(res["error"], "no ps")

    def test_sigterm_ignored_escalates_to_sigkill(self):
        ready = self.tmp / "sigterm-armed"
        p = self.spawn([sys.executable, "-c",
                        "import signal, sys, time\n"
                        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                        "open(sys.argv[1], 'w').write('armed')\n"
                        "time.sleep(60)\n", str(ready)])
        wait_for(lambda: ready.exists(), what="engine armed")   # handler installed
        start = procid.start_time(p.pid)
        res = procid.stop_process_groups(p.pid, start, None, grace=0.3)
        self.assertTrue(res["requested"])
        self.assertGreaterEqual(len(res["groups"]), 1)
        sent = [r["signal"] for r in res["signalled"]]
        self.assertEqual(sent, ["SIGTERM", "SIGKILL"])   # the escalation
        self.assertEqual(res["survivors"], [])
        self.assertIsNone(res["error"])
        wait_for(lambda: not self.alive(p.pid), what="engine gone")

    def test_a_new_session_child_is_stopped_too(self):
        # R0: killpg(engine) alone misses children started with
        # start_new_session=True; every distinct group must be signalled.
        ready = self.tmp / "child-up"
        p = self.spawn([sys.executable, "-c",
                        "import subprocess, sys, time\n"
                        "subprocess.Popen([sys.executable, '-c',\n"
                        "    'import time; time.sleep(60)'], start_new_session=True)\n"
                        "open(sys.argv[1], 'w').write('up')\n"
                        "time.sleep(60)\n", str(ready)])
        wait_for(lambda: ready.exists(), what="tree up")   # the child is spawned
        wait_for(lambda: len(procid.descendants(p.pid)) >= 1, what="child visible")
        start = procid.start_time(p.pid)
        res = procid.stop_process_groups(p.pid, start, None, grace=1.0)
        self.assertTrue(res["requested"])
        # The engine's own group plus at least the child's new group.
        self.assertGreaterEqual(len(res["groups"]), 2, res["groups"])
        self.assertTrue(all(r["signal"] == "SIGTERM" for r in res["signalled"]))
        self.assertEqual(res["survivors"], [])
        wait_for(lambda: not self.alive(p.pid), what="engine gone")
        wait_for(lambda: not procid.descendants(p.pid), what="children gone")

    def test_group_death_between_snapshot_and_signal_is_reported(self):
        # The refusal strings a cancel receipt shows when a group dies first.
        with mock.patch.object(procid, "identity", return_value=None), \
                mock.patch.object(procid, "process_table",
                                  return_value=[procid.Proc(10, 1, 10)]), \
                mock.patch.object(procid, "tree_groups", return_value=[500]), \
                mock.patch.object(procid, "group_members", return_value=[]), \
                mock.patch.object(procid.os, "killpg",
                                  side_effect=ProcessLookupError()):
            res = procid.stop_process_groups(10, None, None)
        self.assertTrue(res["requested"])
        self.assertEqual(res["signalled"],
                         [{"pgid": 500, "signal": "SIGTERM", "result": "no such group"}])

    def test_survivors_of_the_sigkill_wait_are_reported(self):
        # A group that never empties: the SIGKILL wait gives up after ~2 s
        # and the survivors are reported, so cancel stays unconfirmed.
        with mock.patch.object(procid, "identity", return_value=None), \
                mock.patch.object(procid, "process_table",
                                  return_value=[procid.Proc(10, 1, 10)]), \
                mock.patch.object(procid, "tree_groups", return_value=[10]), \
                mock.patch.object(procid, "group_members", return_value=[4242]), \
                mock.patch.object(procid.os, "killpg", return_value=None):
            t0 = time.monotonic()
            res = procid.stop_process_groups(10, None, None, grace=0.1)
            elapsed = time.monotonic() - t0
        self.assertTrue(res["requested"])
        self.assertEqual([s["signal"] for s in res["signalled"]],
                         ["SIGTERM", "SIGKILL"])
        self.assertEqual(res["survivors"], [4242])       # never claimed stopped
        self.assertGreaterEqual(elapsed, 2.0)            # it waited, not spun
        self.assertLess(elapsed, 10.0)
