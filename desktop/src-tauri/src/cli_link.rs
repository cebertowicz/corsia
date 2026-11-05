//! Update existing, recognizable CORSIA CLI links without installing a PATH entry.

#[cfg(unix)]
use std::{fs, io, os::unix::fs::symlink, path::{Path, PathBuf}};

#[cfg(unix)]
fn absolute_link(link: &Path, target: &Path) -> PathBuf {
    if target.is_absolute() { target.to_path_buf() } else { link.parent().unwrap_or(Path::new(".")).join(target) }
}

#[cfg(unix)]
fn resolve_missing(path: &Path) -> io::Result<PathBuf> {
    let mut ancestor = path.to_path_buf();
    let mut missing = Vec::new();
    loop {
        match fs::canonicalize(&ancestor) {
            Ok(mut resolved) => {
                for component in missing.iter().rev() { resolved.push(component); }
                return Ok(resolved);
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                // A dangling symlink is not a missing path component we can safely infer.
                match fs::symlink_metadata(&ancestor) {
                    Err(error) if error.kind() == io::ErrorKind::NotFound => (),
                    _ => return Err(error),
                }
                let Some(name) = ancestor.file_name() else { return Err(error); };
                missing.push(name.to_os_string());
                if !ancestor.pop() { return Err(error); }
            }
            Err(error) => return Err(error),
        }
    }
}

#[cfg(unix)]
#[derive(Clone, Copy)]
pub(crate) enum Repair<'a> {
    Launch(Option<&'a Path>),
    Adoption(&'a Path),
}

#[cfg(unix)]
pub(crate) fn repoint(link: &Path, target: &Path, home: &Path, repair: Repair<'_>) -> io::Result<()> {
    let metadata = match fs::symlink_metadata(link) {
        Ok(metadata) if metadata.file_type().is_symlink() => metadata,
        Ok(_) => return Ok(()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };
    let old = fs::read_link(link)?;
    let resolved = absolute_link(link, &old);
    if resolved == target { return Ok(()); }
    let eligible = match repair {
        Repair::Launch(retired) => {
            let Ok(resolved_path) = resolve_missing(&resolved) else { return Ok(()); };
            let same_as = |path: &Path| resolve_missing(path).is_ok_and(|known| known == resolved_path);
            let recognizable = retired.is_some_and(same_as)
                || same_as(&home.join(".corsia/scripts/corsia"))
                || resolved_path.ends_with("CORSIA.app/Contents/Resources/bin/corsia")
                || resolved_path.ends_with("CORSIA.app/Contents/Resources/scripts/corsia");
            recognizable && matches!(fs::metadata(&resolved), Err(error) if error.kind() == io::ErrorKind::NotFound)
        }
        Repair::Adoption(retired) => fs::metadata(&resolved).is_ok()
            && (resolved == retired || match (fs::canonicalize(&resolved), fs::canonicalize(retired)) {
                (Ok(a), Ok(b)) => a == b,
                _ => false,
            }),
    };
    if !eligible { return Ok(()); }
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
pub(crate) fn repoint_existing(home: &Path, target: &Path, repair: Repair<'_>) {
    if !target.is_file() { return; }
    for link in [home.join(".local/bin/corsia"), PathBuf::from("/opt/homebrew/bin/corsia"), PathBuf::from("/usr/local/bin/corsia")] {
        if let Err(error) = repoint(&link, target, home, repair) {
            log::warn!("could not update CLI link {}: {error}", link.display());
        }
    }
}
