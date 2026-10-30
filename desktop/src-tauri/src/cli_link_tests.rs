use super::cli_link::{repoint, Repair};
use std::{fs, os::unix::fs::symlink};
use tempfile::TempDir;

fn setup() -> (TempDir, std::path::PathBuf, std::path::PathBuf) {
    let root = tempfile::tempdir().unwrap();
    let home = root.path().join("home");
    let target = root.path().join("CORSIA.app/Contents/Resources/bin/corsia");
    fs::create_dir_all(&home).unwrap();
    fs::create_dir_all(target.parent().unwrap()).unwrap();
    fs::write(&target, "app cli").unwrap();
    (root, home, target)
}

#[test]
fn repoints_an_adopted_checkout_at_each_supported_location() {
    let (root, home, target) = setup();
    let source = home.join(".corsia/scripts/corsia");
    fs::create_dir_all(source.parent().unwrap()).unwrap();
    fs::write(&source, "source cli").unwrap();
    for location in ["home/.local/bin/corsia", "opt/homebrew/bin/corsia", "usr/local/bin/corsia"] {
        let link = root.path().join(location);
        fs::create_dir_all(link.parent().unwrap()).unwrap();
        symlink(&source, &link).unwrap();
        repoint(&link, &target, &home, Repair::Adoption(source.as_path())).unwrap();
        assert_eq!(fs::read_link(link).unwrap(), target);
    }
}

#[test]
fn preserves_missing_regular_foreign_and_live_links() {
    let (root, home, target) = setup();
    let link = root.path().join("corsia");
    repoint(&link, &target, &home, Repair::Launch(None)).unwrap();
    assert!(!link.exists());
    fs::write(&link, "mine").unwrap();
    repoint(&link, &target, &home, Repair::Launch(None)).unwrap();
    assert_eq!(fs::read_to_string(&link).unwrap(), "mine");
    fs::remove_file(&link).unwrap();
    for source in [root.path().join("other"), home.join(".corsia/scripts/corsia")] {
        fs::create_dir_all(source.parent().unwrap()).unwrap();
        fs::write(&source, "mine").unwrap();
        symlink(&source, &link).unwrap();
        repoint(&link, &target, &home, Repair::Launch(None)).unwrap();
        assert_eq!(fs::read_link(&link).unwrap(), source);
        fs::remove_file(&link).unwrap();
    }
}

#[test]
fn a_user_relink_after_adoption_is_left_alone_on_launch() {
    let (root, home, target) = setup();
    let source = home.join(".corsia/scripts/corsia");
    let link = root.path().join("corsia");
    fs::create_dir_all(source.parent().unwrap()).unwrap();
    fs::write(&source, "source cli").unwrap();
    symlink(&source, &link).unwrap();
    repoint(&link, &target, &home, Repair::Adoption(&source)).unwrap();
    fs::remove_file(&link).unwrap();
    symlink(&source, &link).unwrap();
    repoint(&link, &target, &home, Repair::Launch(None)).unwrap();
    assert_eq!(fs::read_link(link).unwrap(), source);
}

#[test]
fn resolves_relative_links_to_the_retired_checkout() {
    let (root, home, target) = setup();
    let source = home.join(".corsia/scripts/corsia");
    let bin = home.join(".local/bin");
    fs::create_dir_all(source.parent().unwrap()).unwrap();
    fs::create_dir_all(&bin).unwrap();
    fs::write(&source, "source cli").unwrap();
    let link = bin.join("corsia");
    symlink("../../.corsia/scripts/corsia", &link).unwrap();
    repoint(&link, &target, &home, Repair::Adoption(&source)).unwrap();
    assert_eq!(fs::read_link(link).unwrap(), target);
    drop(root);
}

#[test]
fn adoption_handles_live_retired_links_and_launch_handles_dead_links() {
    let (root, home, target) = setup();
    let source = home.join(".corsia/scripts/corsia");
    let link = root.path().join("corsia");
    symlink(&source, &link).unwrap();
    repoint(&link, &target, &home, Repair::Adoption(&source)).unwrap();
    assert_eq!(fs::read_link(&link).unwrap(), source);
    repoint(&link, &target, &home, Repair::Launch(None)).unwrap();
    assert_eq!(fs::read_link(&link).unwrap(), target);
}

#[test]
fn launch_repairs_a_relative_dead_checkout_link() {
    let (_root, home, target) = setup();
    let bin = home.join(".local/bin");
    fs::create_dir_all(&bin).unwrap();
    let link = bin.join("corsia");
    symlink("../../.corsia/scripts/corsia", &link).unwrap();
    repoint(&link, &target, &home, Repair::Launch(None)).unwrap();
    assert_eq!(fs::read_link(link).unwrap(), target);
}

#[test]
fn launch_preserves_a_relative_link_redirected_by_a_symlinked_parent() {
    let (root, home, target) = setup();
    let foreign = root.path().join("foreign/nested");
    fs::create_dir_all(foreign.join("bin")).unwrap();
    symlink(&foreign, home.join(".local")).unwrap();
    let link = home.join(".local/bin/corsia");
    symlink("../../.corsia/scripts/corsia", &link).unwrap();
    repoint(&link, &target, &home, Repair::Launch(None)).unwrap();
    assert_eq!(fs::read_link(link).unwrap(), std::path::PathBuf::from("../../.corsia/scripts/corsia"));
}

#[test]
fn launch_preserves_a_link_with_an_unreadable_target_parent() {
    use std::os::unix::fs::PermissionsExt;
    let (root, home, target) = setup();
    let blocked = home.join(".corsia");
    fs::create_dir_all(&blocked).unwrap();
    fs::set_permissions(&blocked, fs::Permissions::from_mode(0o000)).unwrap();
    let link = root.path().join("corsia");
    let old = blocked.join("scripts/corsia");
    symlink(&old, &link).unwrap();
    let access_error = fs::metadata(&old).unwrap_err().kind();
    let result = repoint(&link, &target, &home, Repair::Launch(None));
    fs::set_permissions(&blocked, fs::Permissions::from_mode(0o700)).unwrap();
    assert_eq!(access_error, std::io::ErrorKind::PermissionDenied);
    result.unwrap();
    assert_eq!(fs::read_link(link).unwrap(), old);
}

#[test]
fn launch_repairs_a_dead_custom_checkout_only_if_recorded() {
    let (root, home, target) = setup();
    let source = root.path().join("custom-checkout/scripts/corsia");
    let link = root.path().join("corsia");
    symlink(&source, &link).unwrap();
    repoint(&link, &target, &home, Repair::Launch(None)).unwrap();
    assert_eq!(fs::read_link(&link).unwrap(), source);
    repoint(&link, &target, &home, Repair::Launch(Some(&source))).unwrap();
    assert_eq!(fs::read_link(link).unwrap(), target);
}

#[test]
fn only_repoints_recognizable_dead_corsia_links() {
    let (root, home, target) = setup();
    let link = root.path().join("corsia");
    for foreign in [root.path().join("missing"), root.path().join("custom-checkout/scripts/corsia")] {
        symlink(&foreign, &link).unwrap();
        repoint(&link, &target, &home, Repair::Launch(None)).unwrap();
        assert_eq!(fs::read_link(&link).unwrap(), foreign);
        fs::remove_file(&link).unwrap();
    }
    for old in [home.join(".corsia/scripts/corsia"), root.path().join("old/CORSIA.app/Contents/Resources/bin/corsia"), root.path().join("old/CORSIA.app/Contents/Resources/scripts/corsia")] {
        symlink(&old, &link).unwrap();
        repoint(&link, &target, &home, Repair::Launch(None)).unwrap();
        assert_eq!(fs::read_link(&link).unwrap(), target);
        fs::remove_file(&link).unwrap();
    }
}
