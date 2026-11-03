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
        self.assertEqual(pb.maint_cmdline(a), ["root=UUID=abc", "loglevel=3", "ro", "init=" + pb.MAINT_PATH])

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
        self.assertIn("search --no-floppy --fs-uuid --set=root U-U", e)
        self.assertIn("linux /boot/vmlinuz-linux root=UUID=abc ro init=" + pb.MAINT_PATH, e)
        self.assertIn("initrd /boot/intel-ucode.img /boot/initramfs-linux.img", e)
        self.assertTrue(pb.managed_block(e).startswith(pb.MARK_BEGIN))

    def test_verdict(self):
        r = pb.Report()
        r.add("a", "ok", ""); self.assertEqual(r.verdict, "supported")
        r.add("b", "warn", ""); self.assertEqual(r.verdict, "supported-with-warnings")
        r.add("c", "fail", ""); self.assertEqual(r.verdict, "unsupported")
        with self.assertRaises(ValueError):
            pb.plan(r)


if __name__ == "__main__":
    unittest.main()
