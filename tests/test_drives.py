import unittest

from nandwash import plan as pl
from nandwash import selection as sel
from nandwash.drives import Drive, Partition, assign_ids, _judge, short_id


def drive(name, serial, *, model="M", size=10**9, parts=(), transport="sata", rot=False, holders=(), tb=False,
          system=False, ptuuid="", by_path=""):
    d = Drive(path="/dev/" + name, name=name, vendor="ATA", model=model, serial=serial, size=size,
              transport=transport, rotational=rot, removable=False, partitions=list(parts), holders=list(holders),
              is_system=system, thunderbolt=tb, ptuuid=ptuuid, by_path=by_path)
    _judge(d)
    return d


def part(name, fstype, mounts=(), swap=False):
    return Partition(name=name, path="/dev/" + name, devnum="8:1", start=0, size=1, fstype=fstype,
                     mounts=list(mounts), swap=swap)


class Caps(unittest.TestCase):
    def test_unmounted(self):
        d = drive("sdc", "S")
        self.assertTrue(d.can("check") and d.can("offline-chunks") and d.can("full"))
        self.assertFalse(d.can("live"))

    def test_system_disk_with_rw_vfat_still_live(self):
        # the mini: rw FAT /boot/efi + rw ext4 /. Live must be allowed (chunks in the FAT are refused one by one).
        d = drive("sdb", "S", parts=[part("sdb1", "vfat", [("/boot/efi", True)]), part("sdb2", "ext4", [("/", True)])],
                  system=True)
        self.assertTrue(d.can("live"))
        self.assertIn("sdb1 (vfat) skipped: no free-space map", d.caps["live"].reasons)
        self.assertFalse(d.can("offline-chunks") or d.can("full"))
        self.assertIsNone(d.caps["offline-boot"].ok)

    def test_ntfs_rw(self):
        d = drive("sdx", "S", parts=[part("sdx1", "ntfs3", [("/mnt/w", True)])])
        self.assertFalse(d.can("live"))
        self.assertIn("ext4/xfs", d.caps["live"].reasons[0])

    def test_readonly_mount(self):
        d = drive("sdx", "S", parts=[part("sdx1", "ext4", [("/mnt", False)])])
        self.assertTrue(d.can("offline-chunks"))
        self.assertFalse(d.can("full") or d.can("live"))

    def test_swap_blocks_offline(self):
        d = drive("sdx", "S", parts=[part("sdx1", "swap", swap=True)])
        self.assertFalse(d.can("offline-chunks"))

    def test_rotational_and_holders(self):
        self.assertFalse(drive("sdh", "S", rot=True).can("check"))
        self.assertTrue(drive("sdu", "S", rot=True, transport="usb").can("check"))
        self.assertFalse(drive("sdm", "S", holders=["dm-0"]).can("check"))

    def test_tb_no_offline_boot(self):
        d = drive("sdd", "S", tb=True, parts=[part("sdd1", "ext4", [("/d", True)])])
        self.assertFalse(d.caps["offline-boot"].ok)


class Ids(unittest.TestCase):
    def test_stable_and_unique(self):
        a, b = drive("sda", "A"), drive("sdb", "B")
        assign_ids([a, b])
        self.assertTrue(a.id.startswith("nw-") and len(a.id) == 9)
        self.assertNotEqual(a.id, b.id)
        self.assertEqual(a.id, short_id(("A", "M", 10**9)))     # name plays no part
        c = drive("sdz", "A")
        assign_ids([c])
        self.assertEqual(c.id, a.id)

    def test_same_serial_told_apart_by_ptuuid(self):
        a, b = drive("sda", "X", ptuuid="p1"), drive("sdb", "X", ptuuid="p2")
        assign_ids([a, b])
        self.assertNotEqual(a.id, b.id)
        self.assertFalse(a.ambiguous or b.ambiguous)

    def test_same_serial_only_port_is_ambiguous(self):
        a = drive("sda", "X", by_path="usb-1", parts=[])
        b = drive("sdb", "X", by_path="usb-2", parts=[])
        assign_ids([a, b])
        self.assertNotEqual(a.id, b.id)
        self.assertTrue(a.ambiguous and b.ambiguous)
        self.assertTrue(a.can("check"))
        self.assertFalse(a.can("offline-chunks") or a.can("full"))


class Selectors(unittest.TestCase):
    def setUp(self):
        self.sys = drive("sdb", "SYS", model="Micron_M500", system=True,
                         parts=[part("sdb2", "ext4", [("/", True)])])
        self.data = drive("sda", "DATA", model="Micron_M600", parts=[part("sda1", "ext4", [("/mnt/data", True)])])
        self.tb = drive("sdc", "TB", model="T-FORCE 512GB", tb=True)
        self.usb = drive("sde", "USB", transport="usb")
        self.hdd = drive("sdh", "HDD", rot=True)
        self.ds = [self.data, self.sys, self.tb, self.usb, self.hdd]
        assign_ids(self.ds)

    def names(self, s):
        return [d.name for d in s.drives]

    def test_all_skips_ineligible_quietly(self):
        s = sel.resolve(self.ds, "all")
        self.assertEqual(self.names(s), ["sda", "sdb", "sdc", "sde"])
        self.assertEqual(s.refused, [])

    def test_exclusions_and_bus(self):
        self.assertEqual(self.names(sel.resolve(self.ds, "all,-bus:usb")), ["sda", "sdb", "sdc"])
        self.assertEqual(self.names(sel.resolve(self.ds, "-sdc,-bus:usb")), ["sda", "sdb"])
        self.assertEqual(self.names(sel.resolve(self.ds, "bus:tb")), ["sdc"])

    def test_model_glob_serial_id_alias(self):
        self.assertEqual(self.names(sel.resolve(self.ds, "model:*m5*")), ["sdb"])
        self.assertEqual(self.names(sel.resolve(self.ds, "serial:DATA")), ["sda"])
        self.assertEqual(self.names(sel.resolve(self.ds, f"id:{self.tb.id}")), ["sdc"])
        self.assertEqual(self.names(sel.resolve(self.ds, self.tb.id)), ["sdc"])
        self.assertEqual(self.names(sel.resolve(self.ds, "data-m600", {"data-m600": self.data.id})), ["sda"])

    def test_system_disk_rule(self):
        s = sel.resolve(self.ds, "all", write=True)
        self.assertNotIn("sdb", self.names(s))
        self.assertEqual(s.refused[0][0].name, "sdb")
        self.assertIn("sdb", self.names(sel.resolve(self.ds, "sdb", write=True)))
        self.assertIn("sdb", self.names(sel.resolve(self.ds, "serial:SYS", write=True)))
        self.assertNotIn("sdb", self.names(sel.resolve(self.ds, "model:*M500*", write=True)))
        self.assertIn("sdb", self.names(sel.resolve(self.ds, "all", write=False)))

    def test_explicit_ineligible_is_refused_with_reason(self):
        s = sel.resolve(self.ds, "sdh")
        self.assertEqual(s.drives, [])
        self.assertIn("rotational", s.refused[0][1])

    def test_errors(self):
        for bad in ("", "nope", "serial:", "serial:NOPE", "-"):
            with self.assertRaises(sel.SelectorError, msg=bad):
                sel.resolve(self.ds, bad)


class Plans(unittest.TestCase):
    def setUp(self):
        S = Selectors(); S.setUp(); self.S = S

    def test_steps(self):
        S = self.S
        self.assertEqual(pl.steps_for(S.data, "refresh")[0], ["scan", "pick", "live-free", "queue-offline"])
        self.assertEqual(pl.steps_for(S.tb, "refresh")[0], ["scan", "pick", "rewrite-chunks"])
        self.assertEqual(pl.steps_for(S.tb, "refresh-full")[0], ["full-refresh"])
        self.assertEqual(pl.steps_for(S.data, "refresh-full")[0], [])
        self.assertEqual(pl.steps_for(S.data, "check")[0], ["scan", "pick"])

    def test_toml_roundtrip_and_validate(self):
        S = self.S
        p = pl.make(sel.resolve(S.ds, "all", write=True), "refresh", rate_of=None)
        self.assertTrue(p.writes)
        self.assertEqual([d.serial for d in p.drives], ["DATA", "TB", "USB"])
        self.assertTrue(any("SYSTEM" in why for _l, why in p.refused))
        q = pl.from_toml(pl.to_toml(p))
        self.assertEqual([(d.serial, d.steps) for d in q.drives], [(d.serial, d.steps) for d in p.drives])
        pairs, problems = pl.validate(q, S.ds)
        self.assertEqual(problems, [])
        self.assertEqual(len(pairs), 3)

    def test_validate_catches_changes(self):
        S = self.S
        q = pl.from_toml(pl.to_toml(pl.make(sel.resolve(S.ds, "sdc,sda"), "refresh", rate_of=None)))
        now_mounted = drive("sdq", "TB", model="T-FORCE 512GB", tb=True,
                            parts=[part("sdq1", "ext4", [("/x", True)])])
        pairs, problems = pl.validate(q, [now_mounted])
        self.assertEqual(pairs, [])
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("not attached" in x for x in problems))
        self.assertTrue(any("rewrite-chunks needs offline-chunks" in x for x in problems))

    def test_bad_plan_files(self):
        for text in ('nandwash_plan = 2\ngoal="check"', 'nandwash_plan = 1\ngoal="wipe"',
                     'nandwash_plan = 1\ngoal="check"\n[[drive]]\nserial="a"\nmodel="m"\nsize=1\nsteps=["dd"]'):
            with self.assertRaises(ValueError):
                pl.from_toml(text)


if __name__ == "__main__":
    unittest.main()
