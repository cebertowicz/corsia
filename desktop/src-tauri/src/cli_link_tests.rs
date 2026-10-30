use super::cli_link::repoint;
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
        repoint(&link, &target, &home, Some(source.as_path())).unwrap();
        assert_eq!(fs::read_link(link).unwrap(), target);
    }
}

#[test]
fn preserves_missing_regular_foreign_and_live_links() {
    let (root, home, target) = setup();
    let link = root.path().join("corsia");
    repoint(&link, &target, &home, None).unwrap();
    assert!(!link.exists());
    fs::write(&link, "mine").unwrap();
    repoint(&link, &target, &home, None).unwrap();
    assert_eq!(fs::read_to_string(&link).unwrap(), "mine");
    fs::remove_file(&link).unwrap();
    for source in [root.path().join("other"), home.join(".corsia/scripts/corsia")] {
        fs::create_dir_all(source.parent().unwrap()).unwrap();
        fs::write(&source, "mine").unwrap();
        symlink(&source, &link).unwrap();
        repoint(&link, &target, &home, None).unwrap();
        assert_eq!(fs::read_link(&link).unwrap(), source);
        fs::remove_file(&link).unwrap();
    }
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
    repoint(&link, &target, &home, Some(&source)).unwrap();
    assert_eq!(fs::read_link(link).unwrap(), target);
    drop(root);
}

#[test]
fn only_repoints_recognizable_dead_corsia_links() {
    let (root, home, target) = setup();
    let link = root.path().join("corsia");
    let foreign = root.path().join("missing");
    symlink(&foreign, &link).unwrap();
    repoint(&link, &target, &home, None).unwrap();
    assert_eq!(fs::read_link(&link).unwrap(), foreign);
    fs::remove_file(&link).unwrap();
    for old in [home.join(".corsia/scripts/corsia"), root.path().join("old/CORSIA.app/Contents/Resources/bin/corsia"), root.path().join("old/CORSIA.app/Contents/Resources/scripts/corsia")] {
        symlink(&old, &link).unwrap();
        repoint(&link, &target, &home, None).unwrap();
        assert_eq!(fs::read_link(&link).unwrap(), target);
        fs::remove_file(&link).unwrap();
    }
}
