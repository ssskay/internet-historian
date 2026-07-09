# Homebrew formula for Internet Historian.
#
# SCAFFOLD: this is generated for a personal tap (e.g. ssskay/homebrew-tap), not
# for homebrew-core. See docs/homebrew.md for how to publish and test it.
#
# The `url`/`sha256` below point at the v0.3.0 source distribution attached to
# the GitHub release. The `resource` blocks pin `requests` and its dependency
# tree; regenerate them for future versions with:
#     brew update-python-resources internet-historian
class InternetHistorian < Formula
  include Language::Python::Virtualenv

  desc "Quietly preserve the web things you love, forever — patient Internet-Archive queue"
  homepage "https://github.com/ssskay/internet-historian"
  url "https://github.com/ssskay/internet-historian/releases/download/v0.3.0/internet_historian-0.3.0.tar.gz"
  sha256 "ed0ac845ce29417eecad1bbe9f98ba64da5efd8b86403985a6a96db98a4e731d"
  license "MIT"

  depends_on "python@3.12"

  resource "certifi" do
    url "https://files.pythonhosted.org/packages/c9/c7/424b75da314c1045981bd9777432fad05a9e0c69daa4ed7e308bbaffe405/certifi-2026.6.17.tar.gz"
    sha256 "024c88eeec92ca068db80f02b8b07c9cef7b9fe261d1d535abfd5abd6f6af432"
  end

  resource "charset-normalizer" do
    url "https://files.pythonhosted.org/packages/e7/a1/67fe25fac3c7642725500a3f6cfe5821ad557c3abb11c9d20d12c7008d3e/charset_normalizer-3.4.7.tar.gz"
    sha256 "ae89db9e5f98a11a4bf50407d4363e7b09b31e55bc117b4f7d80aab97ba009e5"
  end

  resource "idna" do
    url "https://files.pythonhosted.org/packages/cd/63/9496c57188a2ee585e0f1db071d75089a11e98aa86eb99d9d7618fc1edce/idna-3.18.tar.gz"
    sha256 "ffb385a7e039654cef1ab9ef32c6fafe283c0c0467bba1d9029738ce4a14a848"
  end

  resource "requests" do
    url "https://files.pythonhosted.org/packages/ac/c3/e2a2b89f2d3e2179abd6d00ebd70bff6273f37fb3e0cc209f48b39d00cbf/requests-2.34.2.tar.gz"
    sha256 "f288924cae4e29463698d6d60bc6a4da69c89185ad1e0bcc4104f584e960b9ed"
  end

  resource "urllib3" do
    url "https://files.pythonhosted.org/packages/53/0c/06f8b233b8fd13b9e5ee11424ef85419ba0d8ba0b3138bf360be2ff56953/urllib3-2.7.0.tar.gz"
    sha256 "231e0ec3b63ceb14667c67be60f2f2c40a518cb38b03af60abc813da26505f4c"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    # `--help` needs no network, no Keychain, and no config — a safe smoke test.
    assert_match "preserve the web things you love",
                 shell_output("#{bin}/internet-historian --help")
    # The `historian` alias is installed and can list its subcommands too.
    assert_match "discover", shell_output("#{bin}/historian --help")
  end
end
