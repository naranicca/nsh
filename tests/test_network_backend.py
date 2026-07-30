import unittest

from nsh.network.backend import RemoteBackend, RemoteEntry, parse_target


class FakeBackend(RemoteBackend):
    def __init__(self, names):
        super().__init__("example.com", 22, "user")
        self.names = names

    def listdir(self, path):
        return [RemoteEntry(name, path + "/" + name, False) for name in self.names]


class NetworkBackendTests(unittest.TestCase):
    def test_parse_sftp_target(self):
        self.assertEqual(
            parse_target("sftp", "alice@example.com:2222/home/alice"),
            ("example.com", 2222, "alice", "/home/alice"),
        )

    def test_parse_ftp_defaults(self):
        self.assertEqual(
            parse_target("ftp", "ftp.example.com"),
            ("ftp.example.com", 21, "anonymous", "/"),
        )

    def test_unique_remote_path(self):
        backend = FakeBackend({"report.txt", "report (2).txt"})
        self.assertEqual(
            backend.unique_path("/upload", "report.txt"),
            "/upload/report (3).txt",
        )


if __name__ == "__main__":
    unittest.main()
