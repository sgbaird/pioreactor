import React from 'react'
import {AppBar, Toolbar, Typography} from '@material-ui/core';
import {makeStyles} from '@material-ui/styles';
import Button from '@material-ui/core/Button';


const useStyles = makeStyles(() => ({
    logo: {
    },
    headerOptions:{
      display: "flex",
      flex: 1,
      marginLeft: "100px"
    }
}));


const Header = () => {
    const classes = useStyles();
    return (
    <AppBar position="static">
        <Toolbar variant="dense">
          <Typography className={classes.logo}>
            Morbidostat
          </Typography>
          <div className={classes.headerOptions}>
            <Button color="inherit">Start new experiment</Button>
            <Button color="inherit">Download experiment data</Button>
          </div>
        </Toolbar>
      </AppBar>
    )
}

export default Header;
