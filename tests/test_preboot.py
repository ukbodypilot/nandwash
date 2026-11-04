import os, tempfile, unittest
from nandwash import preboot as pb

MI = """22 1 8:2 / / rw,noatime - ext4 /dev/sdb2 rw
30 22 8:1 / /boot/efi rw - vfat /dev/sdb1 rw
31 22 8:17 / /mnt/data rw - ext4 /dev/sda1 rw
40 22 0:5 / /home rw - xfs /dev/sdc1 rw"""


class T(unittest.TestCase):
    def test_mount_for(self):
        m = pb.parse_mountinfo(MI)
        self.assertEqual(pb.mount_for("/boot/vmlinuz", m)[2], "/dev/sdb2")
        self.assertEqual(pb.mount_for("/boot/efi/EFI/x", m)[2], "/dev/sdb1")
        self.assertEqual(pb.mount_for("/home/u/venv/bin/nandwash", m)[0], "/home")
        self.assertEqual(pb.mount_for("/homestead", m)[0], "/")          # prefix must be a path boundary

    def test_cmdline(self):
        a = pb.parse_cmdline("BOOT_IMAGE=/boot/vmlinuz-linux root=UUID=abc rw quiet splash loglevel=3 init=/sbin/x resume=UUID=zz resume_offset=5")
        self.assertEqual(pb.maint_cmdline(a), ["root=UUID=abc", "loglevel=3", "ro", "panic=30", "init=" + pb.MAINT_PATH])
        self.assertNotIn("panic=0", pb.maint_cmdline(pb.parse_cmdline("root=x panic=0")))

    def test_initrds(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ("vmlinuz-linux", "initramfs-linux.img", "initramfs-linux-fallback.img", "intel-ucode.img"):
                open(os.path.join(d, n), "w").close()
            got = pb.find_initrds(os.path.join(d, "vmlinuz-linux"))
            self.assertEqual([os.path.basename(x) for x in got], ["intel-ucode.img", "initramfs-linux.img"])
            open(os.path.join(d, "vmlinuz-6.1.0"), "w").close()
            open(os.path.join(d, "initrd.img-6.1.0"), "w").close()
            self.assertEqual(os.path.basename(pb.find_initrds(os.path.join(d, "vmlinuz-6.1.0"))[-1]), "initrd.img-6.1.0")
            open(os.path.join(d, "vmlinuz-none"), "w").close()
            self.assertEqual([x for x in pb.find_initrds(os.path.join(d, "vmlinuz-none")) if "ucode" not in x], [])

    def test_entry(self):
        e = pb.render_entry("U-U", "ext4", "/boot/vmlinuz-linux", ["/boot/intel-ucode.img", "/boot/initramfs-linux.img"],
                            ["root=UUID=abc", "ro", "init=" + pb.MAINT_PATH])
        self.assertIn("--id nandwash-maint", e)
        self.assertIn("insmod part_gpt", e)
        self.assertIn("search --no-floppy --fs-uuid --set=root U-U", e)
        self.assertIn("linux /boot/vmlinuz-linux root=UUID=abc ro init=" + pb.MAINT_PATH, e)
        self.assertIn("initrd /boot/intel-ucode.img /boot/initramfs-linux.img", e)
        self.assertTrue(pb.managed_block(e).startswith(pb.MARK_BEGIN))

    def test_conf_quoted(self):
        import subprocess
        c = pb.render_conf({"SERIAL": "AB 12;$(touch /x)", "NW": "/opt/nandwash/venv/bin/nandwash", "MAXRUN": 900})
        out = subprocess.run(["bash", "-c", c + 'printf "%s|%s|%s" "$SERIAL" "$MAXRUN" "$PICK_LIMIT"'],
                             capture_output=True, text=True).stdout
        self.assertEqual(out, "AB 12;$(touch /x)|900|")
        with self.assertRaises(ValueError):
            pb.render_conf({"EVIL": "1"})

    def test_budget(self):
        from array import array
        lat = array("f", [1000.0] * 1000 + [5000.0] * 10)       # 10 chunks at 5x the 1 ms median
        b = pb.time_budget({}, lat)
        self.assertEqual(b["candidates"], 10)
        self.assertEqual(b["median_us"], 1000.0)
        self.assertEqual((b["pick_limit"], b["fix_limit"]), (300, 300))   # floors
        self.assertEqual(b["maxrun"], 660)
        big = pb.time_budget({}, array("f", [1000.0] * 1000 + [400000.0] * 600))
        self.assertGreater(big["pick_limit"], 1400)                       # 2 * 600 * 3 * 0.4 s + 120
        self.assertEqual(big["maxrun"], big["pick_limit"] + big["fix_limit"] + 60)

    def test_validate_scan(self):
        import json
        from array import array
        with tempfile.TemporaryDirectory() as d:
            rep = {"mode": "scan", "complete": True, "serial": "S1", "total_bytes": 4 * 1048576, "chunk_size": 1048576,
                   "created": "2026-11-01T00:00:00", "latency_file": "r.lat"}
            json.dump(rep, open(os.path.join(d, "r.json"), "w"))
            array("f", [900.0, 1000.0, 1100.0, 3000.0]).tofile(open(os.path.join(d, "r.lat"), "wb"))
            from datetime import datetime
            now = datetime.fromisoformat("2026-11-03T00:00:00").timestamp()
            _r, _l, probs, warns = pb.validate_scan(os.path.join(d, "r.json"), "S1", 4 * 1048576, now)
            self.assertEqual((probs, warns), ([], []))
            _r, _l, probs, _w = pb.validate_scan(os.path.join(d, "r.json"), "S2", 8 * 1048576, now)
            self.assertEqual(len(probs), 2)
            _r, _l, _p, warns = pb.validate_scan(os.path.join(d, "r.json"), "S1", 4 * 1048576, now + 30 * 86400)
            self.assertTrue(warns)

    def test_merge_block(self):
        blk = pb.managed_block("menuentry x {\n}\n")
        self.assertEqual(pb.merge_block("", blk), blk)
        mine = "menuentry mine {\n}"
        once = pb.merge_block(mine, blk)
        self.assertEqual(once, mine + "\n" + blk)
        self.assertEqual(pb.merge_block(once, pb.managed_block("menuentry y {\n}\n")).count(pb.MARK_BEGIN), 1)
        self.assertEqual(pb.merge_block(once, ""), mine + "\n")
        with self.assertRaises(ValueError):
            pb.merge_block(pb.MARK_BEGIN + "\nfoo\n", blk)

    def test_apply_writes_atomically(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src"); open(src, "w").write("SRC")
            pl = {"files": [{"path": os.path.join(d, "a/b/run"), "mode": "0755", "src": src},
                            {"path": os.path.join(d, "c.cfg"), "mode": "0644", "data": "DATA\n"}]}
            pb.apply(pl, log=lambda m: None)
            self.assertEqual(open(os.path.join(d, "a/b/run")).read(), "SRC")
            self.assertEqual(os.stat(os.path.join(d, "a/b/run")).st_mode & 0o777, 0o755)
            self.assertEqual(open(os.path.join(d, "c.cfg")).read(), "DATA\n")
            self.assertFalse([x for x in os.listdir(d) if x.endswith(".nw-tmp")])

    def test_verdict(self):
        r = pb.Report()
        r.add("a", "ok", ""); self.assertEqual(r.verdict, "supported")
        r.add("b", "warn", ""); self.assertEqual(r.verdict, "supported-with-warnings")
        r.add("c", "fail", ""); self.assertEqual(r.verdict, "unsupported")
        with self.assertRaises(ValueError):
            pb.plan(r)


class Uninstall(unittest.TestCase):
    def _paths(self, d):
        from unittest import mock
        conf = os.path.join(d, "etc", "nandwash", "maint.conf")
        os.makedirs(os.path.dirname(conf)); open(conf, "w").close()
        grub = os.path.join(d, "grub"); os.makedirs(grub)
        return [mock.patch.object(pb, "CONF_PATH", conf), mock.patch.object(pb, "MAINT_PATH", os.path.join(d, "maint")),
                mock.patch.object(pb, "INPUT_DIR", os.path.join(d, "input"))], conf, grub

    def test_removes_the_conf_dir_it_created(self):
        with tempfile.TemporaryDirectory() as d:
            ps, conf, grub = self._paths(d)
            for x in ps: x.start()
            try:
                pb.uninstall(grub, log=lambda *_: None)
            finally:
                for x in ps: x.stop()
            self.assertFalse(os.path.exists(os.path.dirname(conf)))

    def test_keeps_the_conf_dir_if_something_else_is_in_it(self):
        with tempfile.TemporaryDirectory() as d:
            ps, conf, grub = self._paths(d)
            other = os.path.join(os.path.dirname(conf), "chunks.json"); open(other, "w").close()
            for x in ps: x.start()
            try:
                pb.uninstall(grub, log=lambda *_: None)
            finally:
                for x in ps: x.stop()
            self.assertFalse(os.path.exists(conf))
            self.assertTrue(os.path.exists(other))


if __name__ == "__main__":
    unittest.main()
