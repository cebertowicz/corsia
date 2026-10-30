//! Update existing, recognizable CORSIA CLI links without installing a PATH entry.

#[cfg(unix)]
use std::{fs, io, os::unix::fs::symlink, path::{Path, PathBuf}};

#[cfg(unix)]
fn absolute_link(link: &Path, target: &Path) -> PathBuf {
    if target.is_absolute() { target.to_path_buf() } else { link.parent().unwrap_or(Path::new(".")).join(target) }
}

#[cfg(unix)]
pub(crate) fn repoint(link: &Path, target: &Path, home: &Path, retired: Option<&Path>) -> io::Result<()> {
    let metadata = match fs::symlink_metadata(link) {
        Ok(metadata) if metadata.file_type().is_symlink() => metadata,
        Ok(_) => return Ok(()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };
    let old = fs::read_link(link)?;
    let resolved = absolute_link(link, &old);
    if resolved == target { return Ok(()); }
    let is_retired = retired.is_some_and(|path| {
        resolved == path || match (fs::canonicalize(&resolved), fs::canonicalize(path)) {
            (Ok(a), Ok(b)) => a == b,
            _ => false,
        }
    });
    let recognizable = resolved == home.join(".corsia/scripts/corsia")
        || resolved.ends_with("CORSIA.app/Contents/Resources/bin/corsia")
        || resolved.ends_with("CORSIA.app/Contents/Resources/scripts/corsia");
    let known_dead = recognizable
        && matches!(fs::metadata(&resolved), Err(error) if error.kind() == io::ErrorKind::NotFound);
    if !is_retired && !known_dead { return Ok(()); }
    // Create the replacement in the same directory for an atomic rename.
    // Recheck the existing link immediately before replacing it.
    let parent = link.parent().ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "link has no parent"))?;
    let temporary = parent.join(format!(".corsia-link-{}", std::process::id()));
    symlink(target, &temporary)?;
    let result = (|| {
        use std::os::unix::fs::MetadataExt;
        let current = fs::symlink_metadata(link)?;
        if !current.file_type().is_symlink() || current.ino() != metadata.ino() || current.dev() != metadata.dev() || fs::read_link(link)? != old {
            return Ok(());
        }
        fs::rename(&temporary, link)
    })();
    if let Err(error) = fs::remove_file(&temporary) {
        if error.kind() != io::ErrorKind::NotFound {
            log::warn!("could not remove temporary CLI link {}: {error}", temporary.display());
        }
    }
    result
}

#[cfg(unix)]
pub(crate) fn repoint_existing(home: &Path, target: &Path, retired: Option<&Path>) {
    if !target.is_file() { return; }
    for link in [home.join(".local/bin/corsia"), PathBuf::from("/opt/homebrew/bin/corsia"), PathBuf::from("/usr/local/bin/corsia")] {
        if let Err(error) = repoint(&link, target, home, retired) {
            log::warn!("could not update CLI link {}: {error}", link.display());
        }
    }
}
